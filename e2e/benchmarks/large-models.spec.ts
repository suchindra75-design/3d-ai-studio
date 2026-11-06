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
    const lod = await (
      await request.post(`/api/v1/model-versions/${version.id}/lod`, { timeout: 30 * 60_000 })
    ).json();
    record.lod_build_s = (Date.now() - started) / 1000;
    record.lod_levels = lod.levels.map((l: { triangles: number }) => l.triangles);

    started = Date.now();
    await page.goto(`/projects/${projectId}/models/${modelId}`);
    const preview = page.getByRole('img', { name: /Simplified preview of/ });
    const canvas = page.getByRole('img', { name: /3D view of .*version 2/ });
    await expect(preview.or(canvas)).toBeVisible({ timeout: 10 * 60_000 });
    record.first_view_s = (Date.now() - started) / 1000;
    await expect(canvas).toBeVisible({ timeout: 20 * 60_000 });
    record.full_model_s = (Date.now() - started) / 1000;
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
