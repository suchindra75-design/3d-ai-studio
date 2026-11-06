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
  // back here so the preview can be seen), then the full model takes over.
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route(`**/api/v1/model-versions/${version.id}/file`, async (route) => {
    await held;
    await route.continue();
  });
  await page.reload();
  const preview = page.getByRole('img', { name: /Simplified preview of .*version 2/ });
  await expect(preview).toBeVisible({ timeout: 60_000 });
  await expect(
    page.getByText(/Simplified preview \(81,\d{3} of 1,310,720 triangles\)/),
  ).toBeVisible();
  await settle(page);
  expect(difference(coarse, await preview.screenshot())).toBeLessThan(0.05);
  release();
  await expect(canvas).toBeVisible({ timeout: 60_000 });
  await expect(preview).toHaveCount(0);
});
