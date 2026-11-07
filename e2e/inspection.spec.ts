import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { expect, test, type Page } from '@playwright/test';

import { modelWithVersion } from './fixtures';

// Health inspection (D69) and repairs (D70) in the workspace: inspect a box with a
// missing side, show and locate the hole in the same canvas,
// repair it into a new version, compare, and go back to the original.

const PY = '.venv/bin/python';

function brokenBox(): Buffer {
  const script = [
    'import sys, numpy as np, trimesh',
    'box = trimesh.creation.box(extents=(1, 1, 1))',
    'faces = box.faces[2:]  # one side missing: a four-edge hole',
    'mesh = trimesh.Trimesh(box.vertices, faces, process=False)',
    "sys.stdout.buffer.write(trimesh.Scene(mesh).export(file_type='glb'))",
  ].join('\n');
  return execFileSync(PY, ['-I', '-c', script]);
}

function difference(a: Buffer, b: Buffer): number {
  const folder = mkdtempSync(join(tmpdir(), 'ai3d-inspect-'));
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

test('inspect, highlight, locate and repair a model', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one run is enough');
  test.setTimeout(240_000);
  const { projectId, modelId } = await modelWithVersion(request, 'E2E inspect', brokenBox());
  await page.goto(`/projects/${projectId}/models/${modelId}`);
  const canvas = page.getByRole('img', { name: /3D view of .*version 2/ });
  await expect(canvas).toBeVisible();
  await page.evaluate(() => {
    document.querySelector('canvas')!.dataset.e2eCanvas = 'workspace';
  });
  await page.getByRole('tab', { name: 'Quality' }).click();
  const panel = page.getByLabel('Health inspection');
  await panel.getByRole('button', { name: 'Inspect model' }).click();
  const topology = panel.getByRole('list', { name: 'Topology' });
  await expect(topology.getByText('Holes and open borders')).toBeVisible({ timeout: 60_000 });
  await expect(topology.getByText(/4 edges belong to one triangle only/)).toBeVisible();
  await expect(panel.getByRole('link', { name: 'JSON' })).toBeVisible();

  // Show the hole on the model: drawn in the same canvas, the camera does not move.
  await settle(page);
  const before = await canvas.screenshot();
  const hole = topology.getByRole('listitem').filter({ hasText: 'Holes and open borders' });
  await hole.getByRole('button', { name: 'Show on model' }).click();
  await settle(page);
  const shown = await canvas.screenshot();
  expect(difference(before, shown)).toBeGreaterThan(0.0005);
  await hole.getByRole('button', { name: 'Hide on model' }).click();
  await settle(page);
  expect(difference(before, await canvas.screenshot())).toBeLessThan(0.0005); // camera kept
  // Locate: the camera moves to the opening.
  await hole.getByRole('button', { name: 'Locate' }).click();
  await settle(page);
  expect(difference(before, await canvas.screenshot())).toBeGreaterThan(0.02);
  expect(
    await page.evaluate(() => ({
      same: document.querySelectorAll('canvas[data-e2e-canvas="workspace"]').length,
      canvases: document.querySelectorAll('canvas').length,
    })),
  ).toEqual({ same: 1, canvases: 1 }); // no extra WebGL context

  // Repair: confirm, a new version, the original unchanged and restorable.
  await hole.getByRole('button', { name: 'Fill small holes…' }).click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog.getByText(/version 2 is not changed/)).toBeVisible();
  await dialog.getByRole('button', { name: 'Repair' }).click();
  await expect(panel.getByText('Repaired: saved as a new version.')).toBeVisible({
    timeout: 120_000,
  });
  await panel.getByRole('button', { name: 'Compare before/after' }).click();
  await expect(page.getByRole('img', { name: /3D view of .*version 3/ })).toBeVisible();
  await expect(page.getByRole('img', { name: /version 2/ }).first()).toBeVisible();

  const versions = await (await request.get(`/api/v1/models/${modelId}/versions`)).json();
  const repaired = versions.items.find((v: { version_number: number }) => v.version_number === 3);
  const steps = repaired.quality_report.metrics.enhancement;
  expect(steps[0]).toMatchObject({ code: 'fill_small_holes', status: 'applied' });
  expect(repaired.quality_report.metrics.watertight).toBe(true);
  // Undo: the original is still there and can be made current again.
  const original = versions.items.find((v: { version_number: number }) => v.version_number === 2);
  const restored = await request.patch(`/api/v1/models/${modelId}`, {
    data: { current_version_id: original.id },
  });
  expect((await restored.json()).current_version.version_number).toBe(2);
});
