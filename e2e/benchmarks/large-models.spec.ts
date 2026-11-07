import { readFileSync, writeFileSync } from 'node:fs';
import { basename, dirname } from 'node:path';

import { expect, test, type Page } from '@playwright/test';

import { modelWithVersion } from '../fixtures';

// Browser benchmark for large models (docs/BENCHMARKS.md). Runs only when given
// models: AI3D_BENCHMARK_GLBS=/path/a.glb,/path/b.glb pnpm exec playwright test
// e2e/benchmarks --project=desktop. Records upload, level building, time to the
// first (preview) and full view, the JS heap, and the GPU the browser used. It does
// not time rendering: waiting for animation frames shows when the browser accepts a
// frame, not how long the GPU takes to draw it (see BENCHMARKS.md).

const models = (process.env.AI3D_BENCHMARK_GLBS ?? '').split(',').filter(Boolean);
// Optional: limit the browser's download speed (megabits per second) to see the
// preview and the full model arrive over a slow connection.
const downloadMbps = Number(process.env.AI3D_BENCHMARK_DOWNLOAD_MBPS ?? 0);
test.skip(models.length === 0, 'set AI3D_BENCHMARK_GLBS to benchmark models');
test.use({ viewport: { width: 1440, height: 900 } });

async function frames(page: Page, count: number) {
  await page.evaluate(
    (n) =>
      new Promise<void>((done) => {
        let left = n;
        const next = () => (--left <= 0 ? done() : requestAnimationFrame(next));
        requestAnimationFrame(next);
      }),
    count,
  );
}

for (const path of models) {
  const label = `${basename(dirname(path))}/${basename(path)}`;
  test(`benchmark ${label}`, async ({ page, request }, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop', 'one browser');
    test.setTimeout(40 * 60_000);
    const record: Record<string, unknown> = { model: path };
    const data = readFileSync(path);
    record.glb_mb = Math.round(data.length / 1e5) / 10;

    let started = Date.now();
    const { projectId, modelId } = await modelWithVersion(request, `Bench ${label}`, data);
    record.upload_and_check_s = (Date.now() - started) / 1000;
    const version = (await (await request.get(`/api/v1/models/${modelId}`)).json()).current_version;
    record.triangles = version.stats.triangle_count;

    started = Date.now();
    await request.post(`/api/v1/model-versions/${version.id}/lod`);
    // Built in the background: poll until it is done.
    let lod = await (await request.get(`/api/v1/model-versions/${version.id}/lod`)).json();
    for (let i = 0; lod.status === 'building' && i < 1800; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      lod = await (await request.get(`/api/v1/model-versions/${version.id}/lod`)).json();
    }
    if (lod.status !== 'ready') console.log(`levels of detail: ${lod.status} ${lod.message ?? ''}`);
    record.lod_build_s = (Date.now() - started) / 1000;
    record.lod_levels = lod.levels.map((l: { triangles: number }) => l.triangles);

    if (downloadMbps > 0) {
      const cdp = await page.context().newCDPSession(page);
      await cdp.send('Network.enable');
      await cdp.send('Network.emulateNetworkConditions', {
        offline: false,
        latency: 40,
        downloadThroughput: (downloadMbps * 1e6) / 8,
        uploadThroughput: (downloadMbps * 1e6) / 8,
      });
      record.download_mbps = downloadMbps;
    }
    // Every long main-thread task from the page's start (Long Tasks API).
    await page.addInitScript(() => {
      const w = window as unknown as { longTasks: { start: number; ms: number }[] };
      w.longTasks = [];
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          w.longTasks.push({ start: entry.startTime, ms: Math.round(entry.duration) });
        }
      }).observe({ type: 'longtask', buffered: true });
    });
    started = Date.now();
    await page.goto(`/projects/${projectId}/models/${modelId}`);
    const preview = page.getByRole('img', { name: /Simplified preview of/ });
    const canvas = page.getByRole('img', { name: /3D view of .*version 2/ });
    await expect(preview.or(canvas)).toBeVisible({ timeout: 10 * 60_000 });
    record.first_view_s = (Date.now() - started) / 1000;
    await expect(canvas).toBeVisible({ timeout: 20 * 60_000 });
    record.full_model_s = (Date.now() - started) / 1000;
    const shownAt = await page.evaluate(() => performance.now());
    // Then 15 s more: the finer levels load and the picking index is built (in
    // idle-time slices). Tasks are given as ms, and when they started (s, relative
    // to the full model being shown; negative: before it).
    await page.waitForTimeout(15_000);
    const tasks = await page.evaluate(
      (shown) =>
        (window as unknown as { longTasks: { start: number; ms: number }[] }).longTasks.map(
          (task) => ({ ms: task.ms, at_s: Math.round((task.start - shown) / 100) / 10 }),
        ),
      shownAt,
    );
    // The hand-over from the preview to the full model: from 3 s before it was shown
    // to 1 s after (the first frames).
    record.hand_over_longest_ms = Math.max(
      0,
      ...tasks.filter((t) => t.at_s >= -3 && t.at_s <= 1).map((t) => t.ms),
    );
    record.long_tasks = tasks
      .filter((t) => t.at_s > 1)
      .sort((a, b) => b.ms - a.ms)
      .slice(0, 5);
    const detail = page.getByRole('combobox', { name: 'Detail' });
    if (lod.levels.length) await expect(detail).toBeVisible({ timeout: 5 * 60_000 });
    await frames(page, 5);
    record.renderer = await page.evaluate(() => {
      const gl = document.createElement('canvas').getContext('webgl2');
      const ext = gl?.getExtension('WEBGL_debug_renderer_info');
      return ext ? gl!.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'unknown';
    });
    record.js_heap_mb = await page.evaluate(() =>
      Math.round(
        ((performance as unknown as { memory?: { usedJSHeapSize: number } }).memory
          ?.usedJSHeapSize ?? 0) / 1e6,
      ),
    );

    if (lod.levels.length) {
      await detail.selectOption('auto');
      await page.keyboard.press('f');
      await frames(page, 3);
      record.auto_framed = await detail.locator('option[value="auto"]').textContent();
    }
    console.log(JSON.stringify(record));
    writeFileSync(testInfo.outputPath('benchmark.json'), JSON.stringify(record, null, 2));
  });
}
