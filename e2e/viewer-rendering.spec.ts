import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { expect, test, type Locator, type Page } from '@playwright/test';

import { modelWithVersion } from './fixtures';

// The viewer's tools checked on rendered pixels, not only on their controls.
// Model: a 1 × 1 × 1 textured box and, beside it, a sphere, both saved WITHOUT
// normals (as some tools write them): the views must shade them flat.

const PY = '.venv/bin/python';

function model(): Buffer {
  const script = [
    'import sys, io, numpy as np, trimesh',
    'from PIL import Image',
    'rng = np.random.default_rng(1)',
    'box = trimesh.creation.box(extents=(1, 1, 1))',
    'box.unmerge_vertices()',
    'image = Image.fromarray(rng.integers(60, 200, (64, 64, 3), dtype=np.uint8))',
    'box.visual = trimesh.visual.TextureVisuals(uv=rng.random((len(box.vertices), 2)), image=image)',
    'ball = trimesh.creation.icosphere(subdivisions=3, radius=0.4)',
    'ball.apply_translation([1.2, 0, 0])',
    'scene = trimesh.Scene()',
    "scene.add_geometry(box, geom_name='Bare', node_name='Bare')",
    "scene.add_geometry(ball, geom_name='Ball', node_name='Ball')",
    "data = scene.export(file_type='glb', include_normals=False)",
    'sys.stdout.buffer.write(data)',
  ].join('\n');
  return execFileSync(PY, ['-I', '-c', script]);
}

/** Facts about a PNG of the canvas (background is #0e0e10). */
function stats(png: Buffer, region?: [number, number, number, number]) {
  const folder = mkdtempSync(join(tmpdir(), 'ai3d-render-'));
  const path = join(folder, 'shot.png');
  writeFileSync(path, png);
  const script = [
    'import sys, json, numpy as np',
    'from PIL import Image',
    'img = np.asarray(Image.open(sys.argv[1]).convert("RGB"), float)',
    'r = json.loads(sys.argv[2])',
    'h, w = img.shape[:2]',
    'img = img[int(r[1]*h):int(r[3]*h), int(r[0]*w):int(r[2]*w)] if r else img',
    'bg = np.array([14, 14, 16])',
    'obj = np.abs(img - bg).sum(axis=2) > 30',
    'px = img[obj]',
    'sat = float((px.max(axis=1) - px.min(axis=1)).mean()) if len(px) else 0.0',
    'counts = {}',
    'for c in (px // 32).astype(int): counts[tuple(c)] = counts.get(tuple(c), 0) + 1',
    'big = sum(1 for v in counts.values() if v > 0.03 * len(px))',
    'gx = np.abs(np.diff(img, axis=1)).sum(axis=2)[obj[:, 1:] & obj[:, :-1]]',
    'row = obj[obj.shape[0] // 2]',
    'cols = np.nonzero(row)[0].tolist()',
    "print(json.dumps({'objectPixels': int(obj.sum()), 'mean': float(px.mean()) if len(px) else 0.0,",
    "  'saturation': sat, 'bigColours': big, 'contrast': float(gx.mean()) if len(gx) else 0.0,",
    "  'rowFirst': cols[0] if cols else -1, 'rowLast': cols[-1] if cols else -1, 'width': w, 'height': h}))",
  ].join('\n');
  return JSON.parse(
    execFileSync(PY, ['-I', '-c', script, path, JSON.stringify(region ?? null)]).toString(),
  ) as {
    objectPixels: number;
    mean: number;
    saturation: number;
    bigColours: number;
    contrast: number;
    rowFirst: number;
    rowLast: number;
    width: number;
    height: number;
  };
}

async function shot(canvas: Locator, page: Page): Promise<Buffer> {
  await page.waitForTimeout(250); // the view renders on demand
  return canvas.screenshot();
}

test('viewer tools change what is rendered, and measuring is accurate', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one run is enough');
  test.setTimeout(180_000);
  const { projectId, modelId } = await modelWithVersion(request, 'E2E rendering', model());
  await page.goto(`/projects/${projectId}/models/${modelId}`);
  const canvas = page.getByRole('img', { name: /3D view of .*version 2/ });
  await expect(canvas).toBeVisible();
  await page.getByRole('button', { name: 'Shadows' }).click(); // shadows off: cleaner pixels
  await page.getByRole('button', { name: 'Grid' }).click();

  // Lighting presets render differently.
  await page.getByRole('combobox', { name: 'Lighting' }).selectOption('studio');
  const studio = stats(await shot(canvas, page));
  await page.getByRole('combobox', { name: 'Lighting' }).selectOption('flat');
  const flat = stats(await shot(canvas, page));
  expect(Math.abs(flat.mean - studio.mean)).toBeGreaterThan(3);

  // Solid: the box without normals is shaded flat (several distinct faces, not black).
  await page.keyboard.press('f');
  await page.getByRole('radio', { name: 'Solid' }).click();
  const solidBox = stats(await shot(canvas, page), [0, 0, 0.55, 1]);
  expect(solidBox.mean).toBeGreaterThan(40);
  expect(solidBox.bigColours).toBeGreaterThanOrEqual(2);

  // Normals: colourful, and the normal-less box shows its faces in different colours.
  await page.getByRole('radio', { name: 'Normals' }).click();
  const normals = stats(await shot(canvas, page));
  const normalsBox = stats(await shot(canvas, page), [0, 0, 0.55, 1]);
  expect(normals.saturation).toBeGreaterThan(stats(await shotOf('Solid')).saturation + 20);
  expect(normalsBox.bigColours).toBeGreaterThanOrEqual(3);

  // UV checker on the textured box: a high-contrast pattern.
  await page.getByRole('radio', { name: 'UV' }).click();
  const uv = stats(await shot(canvas, page), [0, 0, 0.55, 1]);
  await page.getByRole('radio', { name: 'Solid' }).click();
  const solid = stats(await shot(canvas, page), [0, 0, 0.55, 1]);
  expect(uv.contrast).toBeGreaterThan(solid.contrast * 2);

  // Section (default: across X at the middle, showing the −X side), in the front view:
  // the sphere (right, x > 0.8) disappears; the box (left) stays.
  await page.keyboard.press('1');
  // The middle band: the controls (top) and the triangle count (bottom) are excluded.
  const right = [0.62, 0.3, 1, 0.8] as [number, number, number, number];
  const left = [0, 0.3, 0.45, 0.8] as [number, number, number, number];
  const before = await shot(canvas, page);
  await page.getByRole('button', { name: 'Section' }).click();
  const sectioned = await shot(canvas, page);
  expect(stats(before, right).objectPixels).toBeGreaterThan(500);
  expect(stats(sectioned, right).objectPixels).toBeLessThan(
    stats(before, right).objectPixels * 0.05,
  );
  expect(stats(sectioned, left).objectPixels).toBeGreaterThan(
    stats(before, left).objectPixels * 0.9,
  );
  await page.getByRole('button', { name: 'Section' }).click();
  expect(stats(await shot(canvas, page), right).objectPixels).toBeGreaterThan(500);

  // Measuring: across the 1.000-wide front face of the box, edge to edge.
  await page.keyboard.press('1'); // front view
  const front = stats(await shot(canvas, page), [0, 0, 0.5, 1]);
  const box = (await canvas.boundingBox())!;
  const scale = box.width / front.width / 0.5; // screenshot px → page px (region is half)
  const y = box.y + box.height / 2;
  await page.keyboard.press('m');
  await page.mouse.click(box.x + (front.rowFirst + 2) * scale * 0.5, y);
  await page.mouse.click(box.x + (front.rowLast - 2) * scale * 0.5, y);
  const measured = Number(
    (await page.getByTestId('measured-distance').textContent())!.split(' ')[0],
  );
  expect(measured).toBeGreaterThan(0.97);
  expect(measured).toBeLessThanOrEqual(1.0);
  await page.keyboard.press('m');

  // Screenshot at 2×: twice the view's pixel size, a real PNG of the model.
  await page.getByRole('combobox', { name: 'Screenshot size' }).selectOption('2');
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Screenshot' }).click(),
  ]);
  const png = readFileSync((await download.path())!);
  const width = png.readUInt32BE(16);
  const height = png.readUInt32BE(20);
  const dpr = await page.evaluate(() => window.devicePixelRatio);
  // Twice the view's drawing size (±1 px: fractional CSS sizes are rounded down).
  expect(Math.abs(width - box.width * dpr * 2)).toBeLessThanOrEqual(1);
  expect(Math.abs(height - box.height * dpr * 2)).toBeLessThanOrEqual(1);
  expect(stats(png).objectPixels).toBeGreaterThan(1000);

  async function shotOf(mode: string): Promise<Buffer> {
    await page.getByRole('radio', { name: mode }).click();
    const image = await shot(canvas, page);
    await page.getByRole('radio', { name: 'Normals' }).click();
    return image;
  }
});

test('comparison views move their cameras together', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one run is enough');
  const { projectId, modelId } = await modelWithVersion(request, 'E2E compare cameras', model());
  await page.goto(`/projects/${projectId}/models/${modelId}`);
  await expect(page.getByRole('img', { name: /3D view of .*version 2/ })).toBeVisible();
  await page.getByRole('tab', { name: 'Versions (2)' }).click();
  await page.getByRole('button', { name: 'Compare' }).click();
  const other = page.getByRole('img', { name: '3D view of version 1, for comparison' });
  await expect(other).toBeVisible();
  const main = page.getByRole('img', { name: /3D view of .*version 2/ });
  await page.waitForTimeout(500);
  const before = await other.screenshot();
  const box = (await main.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 150, box.y + box.height / 2, { steps: 10 });
  await page.mouse.up();
  await page.waitForTimeout(500);
  const after = await other.screenshot();
  expect(after.equals(before)).toBe(false); // the other camera followed the drag
});
