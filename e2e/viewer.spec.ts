import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

import { expect, test, type APIRequestContext } from '@playwright/test';

import { texturedPng } from './fixtures';

// Viewer tools on real WebGL: display modes, lighting, section, measuring,
// screenshots, version comparison and a large (3-million-triangle) model.

async function mockModel(request: APIRequestContext, name: string) {
  const project = await (
    await request.post('/api/v1/projects', {
      data: { name, input_mode: 'single_image', allow_cloud_upload: true },
    })
  ).json();
  const upload = await request.post(`/api/v1/projects/${project.id}/images`, {
    multipart: {
      files: { name: 'p.png', mimeType: 'image/png', buffer: texturedPng(600, 400, 4) },
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
  return { projectId: project.id as string, model };
}

test('view modes, section, measuring, screenshot and comparing versions', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one viewer run is enough');
  const { projectId, model } = await mockModel(request, 'E2E viewer');
  await page.goto(`/projects/${projectId}/models/${model.id}`);
  await expect(page.getByRole('img', { name: /3D view of .*version 1/ })).toBeVisible();

  // Display modes and lighting are real switches.
  for (const mode of ['Normals', 'UV', 'Material']) {
    await page.getByRole('radio', { name: mode }).click();
    await expect(page.getByRole('radio', { name: mode })).toHaveAttribute('aria-checked', 'true');
  }
  await page.getByRole('combobox', { name: 'Lighting' }).selectOption('flat');
  await expect(page.getByRole('combobox', { name: 'Lighting' })).toHaveValue('flat');

  // Section view: a view-only plane with its own controls; the model is unchanged.
  await page.getByRole('button', { name: 'Section' }).click();
  await expect(page.getByLabel('Section view')).toBeVisible();
  await page.getByLabel('Section view').getByRole('radio', { name: 'Y' }).click();
  await expect(page.getByRole('button', { name: /Save as version/ })).toBeDisabled();
  await page.getByRole('button', { name: 'Section' }).click();
  await expect(page.getByLabel('Section view')).toHaveCount(0);

  // Measure between two points on the sphere's surface.
  await page.keyboard.press('m');
  await expect(page.getByLabel('Measurement')).toBeVisible();
  const canvas = page.getByRole('img', { name: /3D view of .*version 1/ });
  const box = (await canvas.boundingBox())!;
  const cx = box.x + box.width / 2;
  const cy = box.y + box.height / 2;
  await page.mouse.click(cx - 40, cy);
  await expect(page.getByText('Click a second point.')).toBeVisible();
  await page.mouse.click(cx + 40, cy + 20);
  const distance = page.getByTestId('measured-distance');
  await expect(distance).toHaveText(/^\d+(\.\d+)? units$/);
  const value = Number((await distance.textContent())!.split(' ')[0]);
  expect(value).toBeGreaterThan(0.05);
  expect(value).toBeLessThan(1.0); // within the 1-unit-wide sphere
  await page.keyboard.press('Escape');
  await expect(page.getByText('Click a first point on the model.')).toBeVisible();
  await page.keyboard.press('m');

  // Screenshot: a real PNG of the view.
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Screenshot' }).click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/-v1\.png$/);
  const png = readFileSync((await download.path())!);
  expect(png.subarray(1, 4).toString('ascii')).toBe('PNG');
  expect(png.length).toBeGreaterThan(5_000); // not an empty canvas

  // Make version 2 (moved), then compare it with version 1 side by side.
  await page
    .getByRole('list', { name: 'Model hierarchy' })
    .getByRole('button', { name: /^geometry_0/ })
    .click();
  const x = page.getByRole('spinbutton', { name: 'Position X' });
  await x.fill('0.75');
  await x.press('Enter');
  await page.getByRole('button', { name: 'Save as version 2' }).click();
  await expect(page.getByText(/Saved as version 2/)).toBeVisible();
  await page.getByRole('tab', { name: 'Versions (2)' }).click();
  await page.getByRole('button', { name: 'Compare' }).click();
  await expect(
    page.getByRole('img', { name: ' 3D view of version 1, for comparison' }),
  ).toBeVisible();
  await expect(page.locator('canvas')).toHaveCount(2);
  const table = page.getByRole('table', { name: 'Version comparison' });
  await expect(table.getByRole('row', { name: /Source Edited Generated/ })).toBeVisible();
  await expect(table.getByRole('row', { name: /Triangles 320 320/ })).toBeVisible();
  await page.getByRole('button', { name: 'Stop comparing' }).first().click();
  await expect(page.locator('canvas')).toHaveCount(1);
});

function bigSphere(): Buffer {
  const script = [
    'import sys, trimesh',
    'sphere = trimesh.creation.uv_sphere(radius=0.5, count=[780, 1000])',
    "sys.stdout.buffer.write(trimesh.Scene(sphere).export(file_type='glb'))",
  ].join('\n');
  return execFileSync('.venv/bin/python', ['-I', '-c', script], { maxBuffer: 512 * 1024 * 1024 });
}

test('a 3-million-triangle model opens, with shadows off, and stays usable', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one large-model run is enough');
  test.setTimeout(240_000);
  const { projectId, model } = await mockModel(request, 'E2E large model');
  const data = bigSphere();
  const saved = await request.post(`/api/v1/models/${model.id}/versions`, {
    multipart: {
      file: { name: 'big.glb', mimeType: 'model/gltf-binary', buffer: data },
      parent_version_id: model.current_version.id,
    },
    timeout: 180_000,
  });
  expect(saved.status(), await saved.text()).toBe(201);
  const triangles = (await saved.json()).quality_report.metrics.triangle_count as number;
  expect(triangles).toBeGreaterThan(3_000_000);

  const started = Date.now();
  await page.goto(`/projects/${projectId}/models/${model.id}`);
  await expect(
    page.getByText(new RegExp(`${triangles.toLocaleString('en-US')} triangles ·`)),
  ).toBeVisible({
    timeout: 120_000,
  });
  const openSeconds = (Date.now() - started) / 1000;
  await expect(page.getByText(/Shadows are off for this large model/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Shadows' })).toHaveAttribute(
    'aria-pressed',
    'false',
  );

  // Still responsive: switching a display mode and selecting take well under a second each.
  const t = Date.now();
  await page.getByRole('radio', { name: 'Wireframe' }).click();
  await expect(page.getByRole('radio', { name: 'Wireframe' })).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await page.getByRole('list', { name: 'Model hierarchy' }).getByRole('button').nth(1).click();
  await expect(page.getByTestId('selection-name')).toBeVisible();
  const interactSeconds = (Date.now() - t) / 1000;
  console.log(
    `large model: ${triangles.toLocaleString('en-US')} triangles, ${(data.length / 1e6).toFixed(0)} MB, opened in ${openSeconds.toFixed(1)} s, mode switch + select ${interactSeconds.toFixed(2)} s`,
  );
  // 0.8–0.9 s on a Mac with a GPU (2.8 s before D45). CI's headless Chromium has no GPU
  // and draws WebGL in software (about 6 s there), so it only guards against hangs.
  expect(interactSeconds).toBeLessThan(process.env.CI ? 30 : 5);
});
