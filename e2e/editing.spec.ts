import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { texturedPng } from './fixtures';

// Structural edits in the workspace on a real two-part, textured GLB: delete,
// mirror, place on ground, replace a texture, UV layout, cut with a plane; then
// the server re-opens the saved file and checks it.
function twoPartModel(): Buffer {
  const script = [
    'import sys, numpy as np, trimesh',
    'from PIL import Image',
    'rng = np.random.default_rng(3)',
    'body = trimesh.creation.box(extents=(1.0, 1.0, 0.6))',
    'body.unmerge_vertices()',
    'image = Image.fromarray(rng.integers(0, 255, (64, 64, 3), dtype=np.uint8))',
    'body.visual = trimesh.visual.TextureVisuals(uv=rng.random((len(body.vertices), 2)), image=image)',
    'lid = trimesh.creation.box(extents=(0.4, 0.2, 0.4))',
    'scene = trimesh.Scene()',
    "scene.add_geometry(body, geom_name='Body', node_name='Body', transform=trimesh.transformations.translation_matrix([1.5, 2.0, 0.0]))",
    "scene.add_geometry(lid, geom_name='Lid', node_name='Lid', transform=trimesh.transformations.translation_matrix([1.5, 2.7, 0.0]))",
    "sys.stdout.buffer.write(scene.export(file_type='glb'))",
  ].join('\n');
  return execFileSync('.venv/bin/python', ['-I', '-c', script]);
}

test('delete, mirror, ground, retexture and cut a model, then save it as a checked version', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one editing run is enough');
  const project = await (
    await request.post('/api/v1/projects', {
      data: { name: 'E2E editing', input_mode: 'single_image', allow_cloud_upload: true },
    })
  ).json();
  const upload = await request.post(`/api/v1/projects/${project.id}/images`, {
    multipart: {
      files: { name: 'box.png', mimeType: 'image/png', buffer: texturedPng(600, 400, 7) },
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
  const model = (await (await request.get(`/api/v1/projects/${project.id}/models`)).json())
    .items[0];
  const saved = await request.post(`/api/v1/models/${model.id}/versions`, {
    multipart: {
      file: { name: 'two-parts.glb', mimeType: 'model/gltf-binary', buffer: twoPartModel() },
      parent_version_id: model.current_version.id,
    },
  });
  expect(saved.status(), await saved.text()).toBe(201);

  await page.goto(`/projects/${project.id}/models/${model.id}`);
  await expect(page.getByRole('img', { name: /3D view of .*version 2/ })).toBeVisible();
  const hierarchy = page.getByRole('list', { name: 'Model hierarchy' });
  const inspector = page.getByRole('complementary', { name: 'Inspector' });

  // Delete the lid; undo brings it back; delete it again.
  await hierarchy.getByRole('button', { name: /^Lid/ }).click();
  await inspector.getByRole('button', { name: 'Delete part' }).click();
  await expect(hierarchy.getByRole('button', { name: /^Lid/ })).toHaveCount(0);
  await page.getByRole('button', { name: 'Undo: Deleted "Lid"' }).click();
  await expect(hierarchy.getByRole('button', { name: /^Lid/ })).toBeVisible();
  await hierarchy.getByRole('button', { name: /^Lid/ }).click();
  await page.keyboard.press('Delete');
  await expect(hierarchy.getByRole('button', { name: /^Lid/ })).toHaveCount(0);

  // Mirror the body, then put the model on the ground at the origin.
  await hierarchy.getByRole('button', { name: /^Body/ }).click();
  await inspector
    .getByRole('group', { name: 'Mirror the part' })
    .getByRole('button', { name: 'Mirror along X' })
    .click();
  await inspector.getByRole('button', { name: 'Centre on origin' }).click();

  // Replace the body's texture with a new image and look at its UV layout.
  await inspector.getByLabel(/Texture image for/).setInputFiles({
    name: 'checker.png',
    mimeType: 'image/png',
    buffer: texturedPng(256, 256, 9),
  });
  await expect(page.getByRole('button', { name: /Undo: Replaced the texture of/ })).toBeVisible();
  await inspector.getByRole('button', { name: 'UV layout' }).click();
  await expect(inspector.getByRole('img', { name: 'UV layout: 12 triangles' })).toBeVisible();

  // Cut the model in half across Y, keeping the top; the cut is filled.
  await page.getByRole('tab', { name: 'Cut' }).click();
  await inspector.getByRole('radio', { name: 'Y' }).click();
  await inspector.getByRole('button', { name: 'Cut', exact: true }).click();
  await expect(inspector.getByText('Cut 1 part, filled 1 closed outline.')).toBeVisible();

  await page.getByRole('button', { name: 'Save as version 3' }).click();
  await expect(page.getByText(/Saved as version 3\. Version 2 is unchanged\./)).toBeVisible();

  const versions = await (await request.get(`/api/v1/models/${model.id}/versions`)).json();
  const v3 = versions.items[0];
  expect(v3.version_number).toBe(3);
  expect(v3.quality_report.status).toBe('pass');
  const m = v3.quality_report.metrics;
  expect(m.watertight).toBe(true);
  expect(m.winding_consistent).toBe(true);
  expect(m.inside_out).toBe(false);
  expect(m.boundary_edges).toBe(0);
  expect(m.texture_count).toBe(1);
  // On the ground at the origin, half as tall (1.0 → 0.5), mirrored body kept.
  expect(m.bbox_min[1]).toBeCloseTo(0.5, 4);
  expect(m.bbox_size).toEqual([
    expect.closeTo(1, 4),
    expect.closeTo(0.5, 4),
    expect.closeTo(0.6, 4),
  ]);
  for (const change of [
    'Deleted "Lid"',
    'Mirrored "Body" along X',
    'Centred the model on the origin',
    'Replaced the texture of',
    'Cut at Y',
  ]) {
    expect(v3.notes).toContain(change);
  }

  // The checked version exports for 3D printing.
  const exported = await (
    await request.post(`/api/v1/model-versions/${v3.id}/exports`, { data: { format: 'stl' } })
  ).json();
  expect(exported.status, exported.error_message).toBe('ready');
});
