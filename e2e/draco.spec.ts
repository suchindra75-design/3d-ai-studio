import { execFileSync } from 'node:child_process';

import { expect, test } from '@playwright/test';

import { texturedPng } from './fixtures';

// A Draco-compressed GLB (KHR_draco_mesh_compression), as some providers return:
// the backend decodes and checks it, and the viewer decodes it with three.js'
// bundled Draco decoder (nothing is fetched from a CDN).
function dracoBox(): Buffer {
  const script = [
    'import sys, numpy as np, trimesh',
    'box = trimesh.creation.box(extents=(0.3, 0.5, 0.2)).subdivide().subdivide()',
    'h = (box.vertices[:, 1] + 0.25) / 0.5',
    'c = np.stack([(1 - h) * 220, h * 40, h * 220, np.full(len(h), 255)], 1).astype(np.uint8)',
    'box.visual = trimesh.visual.ColorVisuals(box, vertex_colors=c)',
    "sys.stdout.buffer.write(trimesh.Scene(box).export(file_type='glb', extension_draco=True))",
  ].join('\n');
  return execFileSync('.venv/bin/python', ['-I', '-c', script]);
}

test('a Draco-compressed model is accepted, checked and shown in 3D', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one viewer check is enough');
  const project = await (
    await request.post('/api/v1/projects', {
      data: { name: 'E2E Draco', input_mode: 'single_image', allow_cloud_upload: true },
    })
  ).json();
  const upload = await request.post(`/api/v1/projects/${project.id}/images`, {
    multipart: {
      files: { name: 'box.png', mimeType: 'image/png', buffer: texturedPng(600, 400, 5) },
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

  const data = dracoBox();
  expect(data.includes('KHR_draco_mesh_compression')).toBe(true);
  const saved = await request.post(`/api/v1/models/${model.id}/versions`, {
    multipart: {
      file: { name: 'draco.glb', mimeType: 'model/gltf-binary', buffer: data },
      parent_version_id: model.current_version.id,
    },
  });
  expect(saved.status(), await saved.text()).toBe(201);

  await page.goto(`/projects/${project.id}/models/${model.id}`);
  await expect(page.getByRole('img', { name: /3D view of .*version 2/ })).toBeVisible();
  await expect(page.getByText('3D view unavailable')).toHaveCount(0);
  const inspector = page.getByRole('complementary', { name: 'Inspector' });
  await expect(inspector.getByText('Width (X)')).toBeVisible();
  await expect(inspector.getByText('0.5 units')).toBeVisible(); // height decoded correctly
});
