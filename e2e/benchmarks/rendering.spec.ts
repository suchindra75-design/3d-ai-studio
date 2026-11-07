/* eslint-disable @typescript-eslint/no-explicit-any -- code passed to page.evaluate works on
   the page's own three.js objects, which have no types on this side. */
import { readFileSync, writeFileSync } from 'node:fs';
import { basename, dirname } from 'node:path';

import { expect, test, type Page } from '@playwright/test';

import { modelWithVersion } from '../fixtures';

// Rendering benchmark (docs/BENCHMARKS.md, "Rendering"). Opt-in:
//   AI3D_BENCHMARK_GLBS=/a.glb,/b.glb [AI3D_BENCHMARK_GPU=high-performance] \
//     pnpm exec playwright test e2e/benchmarks/rendering.spec.ts --project=desktop
//
// Method: the page opts in to the viewer's measurement hook; each frame is rendered
// with renderer.render() directly (not the app's on-demand loop), timed three ways:
//   - GPU: a WebGL2 timer query (EXT_disjoint_timer_query_webgl2) around the render;
//     results flagged disjoint by the driver are discarded;
//   - CPU: performance.now() around renderer.render() (preparing and submitting
//     draw calls; the GPU works asynchronously);
//   - draw calls and triangles from renderer.info.
// Per level and shadow setting: 20 warm-up frames (the first, which uploads that
// level's geometry, is reported on its own), then 60 measured frames while the
// camera orbits slowly. Medians and percentiles are reported, never one frame.

const models = (process.env.AI3D_BENCHMARK_GLBS ?? '').split(',').filter(Boolean);
// Which GPU Chrome uses (on a Mac with two): "integrated" forces the low-power one,
// "high-performance" the dedicated one; unset leaves Chrome's choice (the app asks for
// high performance, as three.js and React Three Fiber do by default).
const gpu = process.env.AI3D_BENCHMARK_GPU ?? '';
const gpuArgs =
  gpu === 'integrated'
    ? ['--force_low_power_gpu']
    : gpu === 'high-performance'
      ? ['--force_high_performance_gpu']
      : [];
test.skip(models.length === 0, 'set AI3D_BENCHMARK_GLBS to benchmark rendering');
test.use({
  viewport: { width: 1440, height: 900 },
  launchOptions: { args: gpuArgs },
});

interface Stats {
  median: number;
  p90: number;
  p95: number;
  max: number;
}

interface LevelResult {
  level: number;
  shadows: boolean;
  triangles: number;
  calls: number;
  firstFrame: { cpuMs: number; gpuMs: number | null };
  cpuMs: Stats;
  gpuMs: Stats | null;
  gpuSamples: number;
  disjoint: number;
}

/** Runs in the page: times renders of one level (see the comment at the top). */
async function measureLevel(
  page: Page,
  label: string,
  level: number,
  warmup: number,
  samples: number,
): Promise<Omit<LevelResult, 'shadows'>> {
  return page.evaluate(
    async ({ label, level, warmup, samples }) => {
      type Probe = {
        views: Record<
          string,
          {
            get: () => { gl: any; scene: any; camera: any };
            lod: { current: { update: (l: number) => void } | null };
          }
        >;
      };
      const view = (window as unknown as { __ai3dViewerProbe: Probe }).__ai3dViewerProbe.views[
        label
      ]!;
      const { gl, scene, camera } = view.get();
      const context = gl.getContext() as WebGL2RenderingContext;
      const timer = context.getExtension('EXT_disjoint_timer_query_webgl2') as {
        TIME_ELAPSED_EXT: number;
        GPU_DISJOINT_EXT: number;
      } | null;
      const lod = view.lod.current;
      const model = scene.getObjectByName('Model');
      // Orbit around the model's centre at the current distance.
      const centre = { x: 0, y: 0, z: 0 };
      model.updateMatrixWorld(true);
      const points: number[][] = [];
      model.traverse((o: any) => {
        if (o.isMesh) {
          o.geometry.computeBoundingBox();
          const b = o.geometry.boundingBox;
          points.push([b.min.x, b.min.y, b.min.z], [b.max.x, b.max.y, b.max.z]);
        }
      });
      for (const axis of [0, 1, 2]) {
        const values = points.map((p) => p[axis]!);
        (centre as any)['xyz'[axis]!] = (Math.min(...values) + Math.max(...values)) / 2;
      }
      const start = camera.position.clone();
      const dx = start.x - centre.x;
      const dz = start.z - centre.z;
      const radius = Math.hypot(dx, dz);
      const angle0 = Math.atan2(dz, dx);
      const cpu: number[] = [];
      const queries: WebGLQuery[] = [];
      let triangles = 0;
      let calls = 0;
      let first = { cpuMs: 0, query: null as WebGLQuery | null };
      const total = warmup + samples;
      for (let i = 0; i < total; i += 1) {
        const angle = angle0 + (i / total) * 0.6;
        camera.position.set(
          centre.x + radius * Math.cos(angle),
          start.y,
          centre.z + radius * Math.sin(angle),
        );
        camera.lookAt(centre.x, centre.y, centre.z);
        camera.updateMatrixWorld();
        if (lod) lod.update(level);
        const query = timer ? context.createQuery() : null;
        if (query && timer) context.beginQuery(timer.TIME_ELAPSED_EXT, query);
        const t0 = performance.now();
        gl.render(scene, camera);
        const t1 = performance.now();
        if (query && timer) context.endQuery(timer.TIME_ELAPSED_EXT);
        context.finish(); // one frame at a time: no overlap between samples
        if (i === 0) first = { cpuMs: t1 - t0, query };
        if (i >= warmup) {
          cpu.push(t1 - t0);
          if (query) queries.push(query);
          triangles = gl.info.render.triangles;
          calls = gl.info.render.calls;
        }
      }
      camera.position.copy(start);
      camera.lookAt(centre.x, centre.y, centre.z);
      // Timer results arrive later; wait for each one.
      const read = async (query: WebGLQuery | null): Promise<number | null> => {
        if (!query || !timer) return null;
        for (let tries = 0; tries < 200; tries += 1) {
          if (context.getQueryParameter(query, context.QUERY_RESULT_AVAILABLE)) break;
          await new Promise((r) => requestAnimationFrame(r));
        }
        if (!context.getQueryParameter(query, context.QUERY_RESULT_AVAILABLE)) return null;
        if (context.getParameter(timer.GPU_DISJOINT_EXT)) return null;
        return (context.getQueryParameter(query, context.QUERY_RESULT) as number) / 1e6;
      };
      const gpu: number[] = [];
      let disjoint = 0;
      for (const query of queries) {
        const ms = await read(query);
        if (ms === null) disjoint += 1;
        else gpu.push(ms);
      }
      const firstGpu = await read(first.query);
      const stats = (values: number[]) => {
        const sorted = [...values].sort((a, b) => a - b);
        const at = (q: number) =>
          sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))]!;
        const round = (v: number) => Math.round(v * 100) / 100;
        return {
          median: round(at(0.5)),
          p90: round(at(0.9)),
          p95: round(at(0.95)),
          max: round(sorted[sorted.length - 1]!),
        };
      };
      return {
        level,
        triangles,
        calls,
        firstFrame: {
          cpuMs: Math.round(first.cpuMs * 100) / 100,
          gpuMs: firstGpu === null ? null : Math.round(firstGpu * 100) / 100,
        },
        cpuMs: stats(cpu),
        gpuMs: gpu.length ? stats(gpu) : null,
        gpuSamples: gpu.length,
        disjoint,
      };
    },
    { label, level, warmup, samples },
  );
}

for (const path of models) {
  const label = `${basename(dirname(path))}/${basename(path)}`;
  test(`rendering ${label}`, async ({ page, request }, testInfo) => {
    test.skip(testInfo.project.name !== 'desktop', 'one browser');
    test.setTimeout(40 * 60_000);
    const { projectId, modelId } = await modelWithVersion(
      request,
      `Render ${label}`,
      readFileSync(path),
    );
    const version = (await (await request.get(`/api/v1/models/${modelId}`)).json()).current_version;
    await request.post(`/api/v1/model-versions/${version.id}/lod`);
    // Built in the background: poll until it is done.
    let lod = await (await request.get(`/api/v1/model-versions/${version.id}/lod`)).json();
    for (let i = 0; lod.status === 'building' && i < 1800; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      lod = await (await request.get(`/api/v1/model-versions/${version.id}/lod`)).json();
    }
    if (lod.status !== 'ready') console.log(`levels of detail: ${lod.status} ${lod.message ?? ''}`);

    await page.addInitScript(() => {
      (window as unknown as { __ai3dViewerProbe: object }).__ai3dViewerProbe = { views: {} };
    });
    await page.goto(`/projects/${projectId}/models/${modelId}`);
    const canvas = page.getByRole('img', { name: /3D view of .*version 2/ });
    await expect(canvas).toBeVisible({ timeout: 20 * 60_000 });
    const detail = page.getByRole('combobox', { name: 'Detail' });
    if (lod.levels.length) await expect(detail).toBeVisible({ timeout: 10 * 60_000 });
    const viewLabel = (await canvas.getAttribute('aria-label'))!;
    await page.getByRole('button', { name: 'Grid' }).click(); // the model alone
    const shadowsButton = page.getByRole('button', { name: 'Shadows' });

    // The GPU the app's own canvas draws with (not a new canvas, which may get another).
    const environment = await page.evaluate((viewLabel) => {
      const probe = (
        window as unknown as {
          __ai3dViewerProbe: { views: Record<string, { get: () => { gl: any } }> };
        }
      ).__ai3dViewerProbe;
      const context = probe.views[viewLabel]!.get().gl.getContext() as WebGL2RenderingContext;
      const info = context.getExtension('WEBGL_debug_renderer_info');
      return {
        renderer: info ? context.getParameter(info.UNMASKED_RENDERER_WEBGL) : 'unknown',
        timerQueries: Boolean(context.getExtension('EXT_disjoint_timer_query_webgl2')),
        devicePixelRatio: window.devicePixelRatio,
        userAgent: navigator.userAgent,
        chromeGpuSwitch: '',
      };
    }, viewLabel);
    environment.chromeGpuSwitch = gpuArgs.join(' ') || 'none';

    const results: LevelResult[] = [];
    const levels = [0, ...lod.levels.map((l: { level: number }) => l.level)];
    for (const shadows of [false, true]) {
      if ((await shadowsButton.getAttribute('aria-pressed')) !== String(shadows)) {
        await shadowsButton.click();
      }
      await page.keyboard.press('f');
      for (const level of levels) {
        results.push({ ...(await measureLevel(page, viewLabel, level, 20, 60)), shadows });
      }
    }
    if ((await shadowsButton.getAttribute('aria-pressed')) === 'true') await shadowsButton.click();

    // Picking (what a click or a measurement does): a ray from the centre of the view
    // against the full model, as the app does it. 2 warm-up rays, then 10 timed.
    await page.keyboard.press('f');
    const picking = await page.evaluate((viewLabel) => {
      const probe = (
        window as unknown as {
          __ai3dViewerProbe: { views: Record<string, { get: () => any }> };
        }
      ).__ai3dViewerProbe;
      const { scene, camera, raycaster } = probe.views[viewLabel]!.get();
      const model = scene.getObjectByName('Model');
      const times: number[] = [];
      let hit = false;
      let firstMs = 0;
      for (let i = 0; i < 12; i += 1) {
        raycaster.setFromCamera({ x: (i % 3) * 0.02, y: 0 }, camera);
        const t0 = performance.now();
        const hits = raycaster.intersectObject(model, true);
        const t1 = performance.now();
        hit = hit || hits.length > 0;
        if (i === 0) firstMs = t1 - t0; // builds the picking index on large meshes
        if (i >= 2) times.push(t1 - t0);
      }
      times.sort((a, b) => a - b);
      return {
        hit,
        firstMs: Math.round(firstMs * 10) / 10,
        medianMs: Math.round(times[5]! * 10) / 10,
        maxMs: Math.round(times[times.length - 1]! * 10) / 10,
      };
    }, viewLabel);

    // Auto switching while zooming out and back in (the app's own frames).
    const transitions: string[] = [];
    if (lod.levels.length) {
      await detail.selectOption('auto');
      await page.keyboard.press('f');
      const box = (await canvas.boundingBox())!;
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      const auto = detail.locator('option[value="auto"]');
      const record = async () => {
        await page.evaluate(
          () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))),
        );
        const text = (await auto.textContent()) ?? '';
        if (transitions[transitions.length - 1] !== text) transitions.push(text);
      };
      for (let i = 0; i < 20; i += 1) {
        await page.mouse.wheel(0, 250);
        await record();
      }
      for (let i = 0; i < 40; i += 1) {
        await page.mouse.wheel(0, -250);
        await record();
      }
    }

    // Screenshots render full detail whatever level is drawn: time to the saved PNG
    // at 2× and 4× the view (Detail left on Auto, framed).
    await page.keyboard.press('f');
    const screenshots: { scale: number; ms: number; width: number; height: number; mb: number }[] =
      [];
    const sizeSelect = page.getByRole('combobox', { name: 'Screenshot size' });
    for (const scale of [2, 4]) {
      await sizeSelect.selectOption(String(scale));
      const started = Date.now();
      const [download] = await Promise.all([
        page.waitForEvent('download', { timeout: 120_000 }),
        page.getByRole('button', { name: 'Screenshot' }).click(),
      ]);
      const png = readFileSync((await download.path())!);
      screenshots.push({
        scale,
        ms: Date.now() - started,
        width: png.readUInt32BE(16),
        height: png.readUInt32BE(20),
        mb: Math.round(png.length / 1e5) / 10,
      });
    }
    await sizeSelect.selectOption('2');

    const memory = await page.evaluate(() => {
      const probe = (
        window as unknown as {
          __ai3dViewerProbe: { views: Record<string, { get: () => { gl: any; scene: any } }> };
        }
      ).__ai3dViewerProbe;
      const view = Object.values(probe.views)[0]!;
      const { gl, scene } = view.get();
      let geometryBytes = 0;
      const seen = new Set<unknown>();
      scene.traverse((o: any) => {
        if (!o.isMesh || seen.has(o.geometry)) return;
        seen.add(o.geometry);
        for (const attribute of Object.values(o.geometry.attributes) as any[]) {
          geometryBytes += attribute.array.byteLength;
        }
        if (o.geometry.index) geometryBytes += o.geometry.index.array.byteLength;
      });
      return {
        jsHeapMb: Math.round(
          ((performance as unknown as { memory?: { usedJSHeapSize: number } }).memory
            ?.usedJSHeapSize ?? 0) / 1e6,
        ),
        geometryBuffersMb: Math.round(geometryBytes / 1e6),
        rendererGeometries: gl.info.memory.geometries,
        rendererTextures: gl.info.memory.textures,
      };
    });

    const record = {
      model: label,
      triangles: version.stats.triangle_count,
      levels: lod.levels.map((l: { triangles: number }) => l.triangles),
      environment,
      viewport: '1440 × 900',
      results,
      autoTransitions: transitions,
      picking,
      screenshots,
      memory,
    };
    console.log(JSON.stringify(record));
    writeFileSync(testInfo.outputPath('rendering.json'), JSON.stringify(record, null, 2));
    expect(results.every((r) => r.triangles > 0)).toBe(true);
  });
}
