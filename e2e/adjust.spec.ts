import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { expect, test } from '@playwright/test';

import { texturedPng } from './fixtures';

// Colour adjustments and tiling, checked in the saved file (not just on screen).
function model(): Buffer {
  const script = [
    'import sys, numpy as np, trimesh',
    'from PIL import Image',
    'rng = np.random.default_rng(5)',
    'body = trimesh.creation.box(extents=(1.0, 1.0, 1.0))',
    'body.unmerge_vertices()',
    'image = Image.fromarray(rng.integers(0, 255, (64, 64, 3), dtype=np.uint8))',
    'body.visual = trimesh.visual.TextureVisuals(uv=rng.random((len(body.vertices), 2)), image=image)',
    'lid = trimesh.creation.box(extents=(0.4, 0.2, 0.4))',
    'lid.visual = trimesh.visual.ColorVisuals(lid, vertex_colors=np.tile([90, 60, 30, 255], (len(lid.vertices), 1)).astype(np.uint8))',
    'scene = trimesh.Scene()',
    "scene.add_geometry(body, geom_name='Body', node_name='Body')",
    "scene.add_geometry(lid, geom_name='Lid', node_name='Lid', transform=trimesh.transformations.translation_matrix([0, 0.7, 0]))",
    "sys.stdout.buffer.write(scene.export(file_type='glb'))",
  ].join('\n');
  return execFileSync('.venv/bin/python', ['-I', '-c', script]);
}

function inspect(glb: Buffer): { saturation: number; uvSpan: number[]; lid: number[] } {
  const folder = mkdtempSync(join(tmpdir(), 'ai3d-adjust-'));
  const path = join(folder, 'saved.glb');
  writeFileSync(path, glb);
  const script = [
    'import sys, json, numpy as np',
    'from pathlib import Path',
    'from ai3d.processing.mesh import load_model_scene',
    'scene = load_model_scene(Path(sys.argv[1]))  # read as the app reads files',
    'by_node = {n: scene.geometry[scene.graph[n][1]] for n in scene.graph.nodes_geometry}',
    "body = next(g for n, g in by_node.items() if 'Body' in n)",
    "lid = next(g for n, g in by_node.items() if 'Lid' in n)",
    'img = np.asarray(body.visual.material.baseColorTexture.convert("RGB"), float)',
    'sat = float((img.max(axis=2) - img.min(axis=2)).mean())',
    'uv = body.visual.uv',
    'span = (uv.max(axis=0) - uv.min(axis=0)).tolist()',
    'c = np.asarray(lid.visual.vertex_colors)[:, :3].mean(axis=0).tolist()',
    "print(json.dumps({'saturation': sat, 'uvSpan': span, 'lid': c}))",
  ].join('\n');
  return JSON.parse(execFileSync('.venv/bin/python', ['-I', '-c', script, path]).toString());
}

test('adjust texture colours, tiling and vertex colours, then save them', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one run is enough');
  const project = await (
    await request.post('/api/v1/projects', {
      data: { name: 'E2E adjust', input_mode: 'single_image', allow_cloud_upload: true },
    })
  ).json();
  const upload = await request.post(`/api/v1/projects/${project.id}/images`, {
    multipart: {
      files: { name: 'p.png', mimeType: 'image/png', buffer: texturedPng(400, 300, 3) },
    },
  });
  const imageId = (await upload.json()).results[0].image.id as string;
  const job = await (
    await request.post(`/api/v1/projects/${project.id}/generations`, {
      data: { inputs: [{ image_id: imageId }], provider: 'mock', cloud_upload_consent: true },
    })
  ).json();
  await expect
    .poll(async () => (await (await request.get(`/api/v1/jobs/${job.id}`)).json()).status)
    .toBe('succeeded');
  const generated = (await (await request.get(`/api/v1/projects/${project.id}/models`)).json())
    .items[0];
  const saved = await request.post(`/api/v1/models/${generated.id}/versions`, {
    multipart: {
      file: { name: 'two.glb', mimeType: 'model/gltf-binary', buffer: model() },
      parent_version_id: generated.current_version.id,
    },
  });
  expect(saved.status(), await saved.text()).toBe(201);
  const before = inspect(await (await request.get((await saved.json()).file_url)).body());
  expect(before.saturation).toBeGreaterThan(50);

  await page.goto(`/projects/${project.id}/models/${generated.id}`);
  await expect(page.getByRole('img', { name: /3D view of .*version 2/ })).toBeVisible();
  const hierarchy = page.getByRole('list', { name: 'Model hierarchy' });
  const inspector = page.getByRole('complementary', { name: 'Inspector' });

  // Body: no colour left in its texture, and a 3 × 2 tiling.
  await hierarchy.getByRole('button', { name: /^Body/ }).click();
  await inspector.getByText('Adjust colours and tiling').click();
  await inspector.getByRole('slider', { name: 'Saturation' }).fill('-1');
  await expect(inspector.getByText(/Preview only \(reduced resolution\)/)).toBeVisible();
  await inspector.getByRole('button', { name: 'Apply colours' }).click();
  await expect(page.getByRole('button', { name: /Undo: Adjusted the texture of/ })).toBeVisible();
  await inspector.getByLabel('Repeat U').fill('3');
  await inspector.getByLabel('Repeat V').fill('2');
  await inspector.getByRole('button', { name: 'Apply tiling' }).click();
  await expect(page.getByRole('button', { name: /Undo: Tiled the texture of/ })).toBeVisible();

  // Lid: brighter vertex colours.
  await hierarchy.getByRole('button', { name: /^Lid/ }).click();
  await inspector.getByText('Adjust colours and tiling').click(); // another material: closed
  await expect(inspector.getByText('Vertex colours')).toBeVisible();
  await inspector.getByRole('slider', { name: 'Brightness' }).fill('0.5');
  await inspector.getByRole('button', { name: 'Apply colours' }).click();

  await page.getByRole('button', { name: 'Save as version 3' }).click();
  await expect(page.getByText(/Saved as version 3/)).toBeVisible();
  const versions = await (await request.get(`/api/v1/models/${generated.id}/versions`)).json();
  const v3 = versions.items[0];
  expect(v3.quality_report.status).not.toBe('fail');
  for (const change of [
    'Adjusted the texture of',
    'Tiled the texture of',
    'Adjusted the colours of',
  ]) {
    expect(v3.notes).toContain(change);
  }
  const after = inspect(await (await request.get(v3.file_url)).body());
  expect(after.saturation).toBeLessThan(2); // grey
  expect(after.uvSpan[0]! / before.uvSpan[0]!).toBeCloseTo(3, 1);
  expect(after.uvSpan[1]! / before.uvSpan[1]!).toBeCloseTo(2, 1);
  expect(after.lid[0]!).toBeGreaterThan(before.lid[0]! * 1.3); // brighter
});
