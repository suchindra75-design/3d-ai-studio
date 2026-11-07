import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { expect, test, type Page } from '@playwright/test';

import { modelWithVersion } from './fixtures';

// Levels of detail (D58) on a 1.3-million-triangle model with vertex colours
// (orange top, blue bottom): built by the backend, drawn when full detail is not
// visible, and never used for picking. Rendering speed is measured separately
// (e2e/benchmarks), with the browser's frame-rate limit off.

const PY = '.venv/bin/python';

function largeBall(): Buffer {
  const script = [
    'import sys, numpy as np, trimesh',
    'ball = trimesh.creation.icosphere(subdivisions=8, radius=0.5)',
    'v = ball.vertices',
    'ball.vertices = v * (1 + 0.02 * np.sin(9 * v[:, :1]) * np.cos(7 * v[:, 1:2]))',
    'top = ball.vertices[:, 1] > 0',
    'colours = np.where(top[:, None], [230, 120, 20, 255], [30, 60, 220, 255]).astype(np.uint8)',
    'ball.visual = trimesh.visual.ColorVisuals(ball, vertex_colors=colours)',
    "scene = trimesh.Scene(); scene.add_geometry(ball, geom_name='Ball', node_name='Ball')",
    "sys.stdout.buffer.write(scene.export(file_type='glb'))",
  ].join('\n');
  return execFileSync(PY, ['-I', '-c', script], { maxBuffer: 200 * 1024 * 1024 });
}

/** Share of pixels that differ clearly between two screenshots of the same view. */
function difference(a: Buffer, b: Buffer): number {
  const folder = mkdtempSync(join(tmpdir(), 'ai3d-lod-'));
  writeFileSync(join(folder, 'a.png'), a);
  writeFileSync(join(folder, 'b.png'), b);
  const script = [
    'import sys, numpy as np',
    'from PIL import Image',
    'a = np.asarray(Image.open(sys.argv[1]).convert("RGB"), float)',
    'b = np.asarray(Image.open(sys.argv[2]).convert("RGB"), float)',
    'print(float((np.abs(a - b).sum(axis=2) > 60).mean()))',
  ].join('\n');
  return Number(
    execFileSync(PY, ['-I', '-c', script, join(folder, 'a.png'), join(folder, 'b.png')]),
  );
}

async function settle(page: Page) {
  await page.evaluate(
    () => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done))),
  );
}

test('levels of detail for a 1.3-million-triangle model', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one run is enough');
  test.setTimeout(300_000);
  const { projectId, modelId } = await modelWithVersion(request, 'E2E levels', largeBall());
  const version = (await (await request.get(`/api/v1/models/${modelId}`)).json()).current_version;
  expect(version.stats.triangle_count).toBe(1_310_720);

  await page.goto(`/projects/${projectId}/models/${modelId}`);
  const canvas = page.getByRole('img', { name: /3D view of .*version 2/ });
  await expect(canvas).toBeVisible({ timeout: 60_000 });
  // Built once by the backend, then loaded; the control appears when they are ready.
  const detail = page.getByRole('combobox', { name: 'Detail' });
  await expect(detail).toBeVisible({ timeout: 120_000 });
  const info = await (await request.get(`/api/v1/model-versions/${version.id}/lod`)).json();
  expect(info.status).toBe('ready');
  expect(info.levels.map((l: { triangles: number }) => l.triangles)).toEqual([
    expect.closeTo(327_680, -4),
    expect.closeTo(81_920, -3),
  ]);
  await expect(detail.locator('option')).toHaveText([
    /^Auto \(/,
    'Full (1.3M)',
    /^Level 1 \(32\dK\)$/,
    /^Level 2 \(8\dK\)$/,
  ]);
  await page.getByRole('button', { name: 'Grid' }).click();
  await page.getByRole('button', { name: 'Shadows' }).click(); // already off: large model
  await page.getByRole('button', { name: 'Shadows' }).click();

  // Auto: far away the coarsest level, close up full detail.
  const autoOption = detail.locator('option[value="auto"]');
  const box = (await canvas.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let i = 0; i < 25; i += 1) await page.mouse.wheel(0, 400);
  await settle(page);
  await expect(autoOption).toHaveText(/^Auto \(8\dK\)$/);
  for (let i = 0; i < 45; i += 1) await page.mouse.wheel(0, -400);
  await settle(page);
  await expect(autoOption).toHaveText('Auto (1.3M)');

  // A level looks like the full model from the same view.
  await page.keyboard.press('f');
  await detail.selectOption('0');
  await settle(page);
  const full = await canvas.screenshot();
  await detail.selectOption('2');
  await settle(page);
  const coarse = await canvas.screenshot();
  const changed = difference(full, coarse);
  testInfo.annotations.push({
    type: 'pixels changed (full vs level 2)',
    description: `${changed}`,
  });
  expect(changed).toBeLessThan(0.03);

  // Picking still goes to the full model (the copies are never picked).
  await canvas.click({ position: { x: box.width / 2, y: box.height / 2 } });
  await expect(page.getByTestId('selection-name')).toHaveText('Ball');

  // Opening it again: the stored coarsest level shows while the full file loads (held
  // back here so the preview can be seen), then the full model takes over in the same
  // canvas (D67). Three times, to show the hand-over is reliable.
  await page.addInitScript(() => {
    (window as unknown as { __ai3dViewerProbe: object }).__ai3dViewerProbe = { views: {} };
  });
  let release: () => void = () => {};
  let held: Promise<void> = Promise.resolve();
  await page.route(`**/api/v1/model-versions/${version.id}/file`, async (route) => {
    await held;
    await route.continue();
  });
  const levelFiles: string[] = [];
  page.on('request', (r) => {
    const match = /\/lod\/(\d+)\/file/.exec(r.url());
    if (match) levelFiles.push(match[1]!);
  });
  const preview = page.getByRole('img', { name: /Simplified preview of .*version 2/ });
  const geometries: number[] = [];
  for (let round = 0; round < 3; round += 1) {
    held = new Promise<void>((resolve) => (release = resolve));
    levelFiles.length = 0;
    await page.reload();
    await expect(preview).toBeVisible({ timeout: 60_000 });
    await expect(
      page.getByText(/Simplified preview \(81,\d{3} of 1,310,720 triangles\)/),
    ).toBeVisible();
    await settle(page);
    expect(difference(coarse, await preview.screenshot())).toBeLessThan(0.05);
    // Only the small coarsest level has been fetched: the finer ones would take
    // bandwidth from the full file on a slow connection, so they come after it.
    expect(levelFiles).toEqual([String(info.levels.length)]);

    // Watch the hand-over: the canvas element, context losses, and every frame drawn
    // (the pixel at the centre, which is on the model, and one in a corner).
    await page.evaluate(() => {
      const w = window as unknown as {
        contextLosses: number;
        drawn: { centre: number[]; corner: number[] }[];
        __ai3dViewerProbe: { views: Record<string, { get: () => { gl: unknown } }> };
      };
      const element = document.querySelector('canvas')!;
      element.dataset.e2eCanvas = 'preview';
      w.contextLosses = 0;
      element.addEventListener('webglcontextlost', () => (w.contextLosses += 1));
      w.drawn = [];
      const gl = Object.values(w.__ai3dViewerProbe.views)[0]!.get().gl as {
        render: (scene: unknown, camera: unknown) => void;
        getContext: () => WebGL2RenderingContext;
      };
      const render = gl.render.bind(gl);
      gl.render = (scene, camera) => {
        render(scene, camera);
        const context = gl.getContext();
        const read = (x: number, y: number) => {
          const pixel = new Uint8Array(4);
          context.readPixels(x, y, 1, 1, context.RGBA, context.UNSIGNED_BYTE, pixel);
          return [...pixel];
        };
        const width = context.drawingBufferWidth;
        const height = context.drawingBufferHeight;
        w.drawn.push({ centre: read(width >> 1, height >> 1), corner: read(2, height - 3) });
      };
    });
    // Turn the camera during the preview; it stays turned when the full model arrives.
    const previewBox = (await preview.boundingBox())!;
    const [px, py] = [previewBox.x + previewBox.width / 2, previewBox.y + previewBox.height / 2];
    await page.mouse.move(px, py);
    await page.mouse.down();
    await page.mouse.move(px + 160, py + 40, { steps: 8 });
    await page.mouse.up();
    await settle(page);
    const turned = await preview.screenshot();
    expect(difference(coarse, turned)).toBeGreaterThan(0.01); // the camera did move
    const handOver = Date.now();
    release();
    await expect(canvas).toBeVisible({ timeout: 60_000 });
    await expect(preview).toHaveCount(0);
    await expect.poll(() => [...levelFiles].sort()).toEqual(['1', '2']);
    await settle(page);
    const seen = await page.evaluate(() => {
      const w = window as unknown as {
        contextLosses: number;
        drawn: { centre: number[]; corner: number[] }[];
      };
      return {
        sameCanvas: document.querySelectorAll('canvas[data-e2e-canvas="preview"]').length,
        canvases: document.querySelectorAll('canvas').length,
        losses: w.contextLosses,
        frames: w.drawn.length,
        // A frame without the model: nothing drawn at the centre, or the centre the
        // same as the background in the corner.
        empty: w.drawn.filter(
          ({ centre, corner }) =>
            centre[3] === 0 || centre.every((v, i) => Math.abs(v - corner[i]!) < 8),
        ).length,
      };
    });
    testInfo.annotations.push({
      type: `hand-over ${round + 1}`,
      description: JSON.stringify({ ...seen, ms: Date.now() - handOver }),
    });
    expect(seen).toMatchObject({ sameCanvas: 1, canvases: 1, losses: 0, empty: 0 });
    expect(seen.frames).toBeGreaterThan(0);
    const swapped = difference(turned, await canvas.screenshot());
    testInfo.annotations.push({
      type: `pixels changed, turned preview vs full (${round + 1})`,
      description: `${swapped}`,
    });
    expect(swapped).toBeLessThan(0.05);
    geometries.push(
      await page.evaluate(
        () =>
          Object.values(
            (
              window as unknown as {
                __ai3dViewerProbe: {
                  views: Record<
                    string,
                    { get: () => { gl: { info: { memory: { geometries: number } } } } }
                  >;
                };
              }
            ).__ai3dViewerProbe.views,
          )[0]!.get().gl.info.memory.geometries,
      ),
    );
  }
  // The same GPU geometry each time: nothing kept from earlier rounds or duplicated.
  testInfo.annotations.push({
    type: 'GPU geometries after each hand-over',
    description: `${geometries}`,
  });
  expect(new Set(geometries).size).toBe(1);

  // After the hand-over: every level still draws (none was freed with the preview),
  // and picking, measuring and the section plane work on the full model.
  await page.keyboard.press('f');
  await settle(page);
  const framed = await canvas.screenshot();
  for (const level of ['0', '1', '2']) {
    await detail.selectOption(level);
    await settle(page);
    expect(difference(framed, await canvas.screenshot())).toBeLessThan(0.05);
  }
  await detail.selectOption('auto');
  const after = (await canvas.boundingBox())!;
  await canvas.click({ position: { x: after.width / 2, y: after.height / 2 } });
  await expect(page.getByTestId('selection-name')).toHaveText('Ball');
  await page.keyboard.press('Escape');
  await page.keyboard.press('m');
  await expect(page.getByLabel('Measurement')).toBeVisible();
  const [mx, my] = [after.x + after.width / 2, after.y + after.height / 2];
  await page.mouse.click(mx - 40, my);
  await expect(page.getByText('Click a second point.')).toBeVisible();
  await page.mouse.click(mx + 40, my + 20);
  const distance = page.getByTestId('measured-distance');
  await expect(distance).toHaveText(/^\d+(\.\d+)? units$/);
  const value = Number((await distance.textContent())!.split(' ')[0]);
  expect(value).toBeGreaterThan(0.05);
  expect(value).toBeLessThan(1.0); // within the 1-unit-wide ball
  await page.keyboard.press('Escape');
  await page.keyboard.press('m');
  await page.getByRole('button', { name: 'Section' }).click();
  await expect(page.getByLabel('Section view')).toBeVisible();
  await settle(page);
  expect(difference(framed, await canvas.screenshot())).toBeGreaterThan(0.05); // cut open
  await page.getByRole('button', { name: 'Section' }).click();
  await expect(page.getByLabel('Section view')).toHaveCount(0);
  await settle(page);
  expect(difference(framed, await canvas.screenshot())).toBeLessThan(0.05);
});
