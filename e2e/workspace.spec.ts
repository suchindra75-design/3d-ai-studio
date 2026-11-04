import { expect, test, type APIRequestContext } from '@playwright/test';

import { texturedPng } from './fixtures';

// Uses the test-only mock provider (AI3D_ENVIRONMENT=test): a placeholder
// sphere, never real generation. 1,280 triangles in High quality mode.

async function generatedModel(request: APIRequestContext, name: string) {
  const project = await (
    await request.post('/api/v1/projects', {
      data: { name, input_mode: 'single_image', allow_cloud_upload: true },
    })
  ).json();
  const upload = await request.post(`/api/v1/projects/${project.id}/images`, {
    multipart: {
      files: { name: 'mug.png', mimeType: 'image/png', buffer: texturedPng(800, 600, 11) },
    },
  });
  const imageId = (await upload.json()).results[0].image.id as string;
  const job = await (
    await request.post(`/api/v1/projects/${project.id}/generations`, {
      data: {
        inputs: [{ image_id: imageId }],
        provider: 'mock',
        quality_mode: 'high_quality',
        cloud_upload_consent: true,
      },
    })
  ).json();
  await expect
    .poll(async () => (await (await request.get(`/api/v1/jobs/${job.id}`)).json()).status, {
      timeout: 45_000,
    })
    .toBe('succeeded');
  const models = await (await request.get(`/api/v1/projects/${project.id}/models`)).json();
  return { projectId: project.id as string, modelId: models.items[0].id as string };
}

test('inspect a generated model in 3D, edit it and save a new version', async ({
  page,
  request,
}, testInfo) => {
  const { projectId, modelId } = await generatedModel(request, `E2E 3D ${testInfo.project.name}`);

  // Opened from the project page.
  await page.goto(`/projects/${projectId}`);
  await page.getByRole('link', { name: /Open .* in 3D/ }).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${projectId}/models/${modelId}$`));

  // A real WebGL canvas, not the fallback message.
  await expect(page.getByRole('img', { name: /3D view of .*version 1/ })).toBeVisible();
  await expect(page.locator('canvas')).toBeVisible();
  await expect(page.getByText('3D view unavailable')).toHaveCount(0);
  expect(await page.evaluate(() => !!document.createElement('canvas').getContext('webgl2'))).toBe(
    true,
  );

  const inspector = page.getByRole('complementary', { name: 'Inspector' });
  await expect(inspector.getByText('1,280', { exact: true })).toBeVisible(); // triangles
  await expect(inspector.getByText('AI-generated: unseen surfaces invented')).toBeVisible();
  await expect(inspector.getByText(/no real-world scale/)).toBeVisible();

  // Display modes and helpers are real toggles.
  await page.getByRole('radio', { name: 'Wireframe' }).click();
  await expect(page.getByRole('radio', { name: 'Wireframe' })).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await page.getByRole('button', { name: 'Bounds' }).click();
  await expect(page.getByRole('button', { name: 'Bounds' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );

  // Select the part and move it numerically; undo and redo it.
  const hierarchy = page.getByRole('list', { name: 'Model hierarchy' });
  await hierarchy.getByRole('button', { name: /^geometry_0/ }).click();
  await expect(page.getByTestId('selection-name')).toHaveText('geometry_0');
  const x = page.getByRole('spinbutton', { name: 'Position X' });
  await x.fill('0.5');
  await x.press('Enter');
  const save = page.getByRole('button', { name: 'Save as version 2' });
  await expect(save).toBeEnabled();
  await expect(page.getByText(/Unsaved edits/)).toBeVisible();
  await page.getByRole('button', { name: 'Undo: Moved "geometry_0"' }).click();
  await expect(save).toBeDisabled();
  await page.getByRole('button', { name: 'Redo: Moved "geometry_0"' }).click();
  await expect(page.getByRole('spinbutton', { name: 'Position X' })).toHaveValue('0.5');

  await save.click();
  await expect(page.getByText(/Saved as version 2\. Version 1 is unchanged\./)).toBeVisible();
  await expect(page.getByText(/Version 2 · Edited · current/)).toBeVisible();

  // The backend stored a validated GLB; version 1 is untouched.
  const history = await (await request.get(`/api/v1/models/${modelId}/versions`)).json();
  expect(history.items.map((v: { version_number: number }) => v.version_number)).toEqual([2, 1]);
  const [v2, v1] = history.items;
  expect(v2.source).toBe('edited');
  expect(v2.surface_provenance).toBe('inferred');
  expect(v2.quality_report.status).toBe('pass');
  expect(v2.notes).toContain('Moved "geometry_0"');
  const file = await request.get(v2.file_url);
  expect((await file.body()).subarray(0, 4).toString('ascii')).toBe('glTF');
  expect(v1.source).toBe('generated');

  // Version history: switch back to version 1 as the current version.
  await page.getByRole('tab', { name: 'Versions (2)' }).click();
  const versions = page.getByRole('list', { name: 'Version history' });
  await versions.getByRole('button', { name: 'Make current' }).click();
  await expect(versions.getByRole('listitem').nth(1).getByText('Current')).toBeVisible();
  await versions.getByRole('button', { name: 'View' }).click();
  await expect(page.getByRole('img', { name: /3D view of .*version 1/ })).toBeVisible();

  await page.getByRole('link', { name: /Back to/ }).click();
  await expect(page.getByText('version 1 of 2')).toBeVisible();
});

test('an unknown model shows a not-found error', async ({ page, request }) => {
  const project = await (
    await request.post('/api/v1/projects', {
      data: { name: 'E2E empty', input_mode: 'single_image' },
    })
  ).json();
  await page.goto(`/projects/${project.id}/models/00000000-0000-4000-8000-000000000000`);
  const alert = page.getByRole('alert').filter({ hasText: 'Not found' });
  await expect(alert).toContainText('Model not found.');
});

test('enhance with a measurement, then export verified files', async ({
  page,
  request,
}, testInfo) => {
  const { projectId, modelId } = await generatedModel(
    request,
    `E2E enhance ${testInfo.project.name}`,
  );
  await page.goto(`/projects/${projectId}/models/${modelId}`);
  await expect(page.getByRole('img', { name: /3D view of .*version 1/ })).toBeVisible();

  await page.getByRole('tab', { name: 'Enhance' }).click();
  await page.getByLabel('Quality', { exact: true }).selectOption({ label: 'Professional' });
  // Retopology is an option (off by default), with its fidelity rule stated.
  await expect(page.getByLabel('Retopology')).toHaveValue('none');
  await page.getByLabel('Retopology').selectOption('quadriflow');
  await expect(page.getByText(/kept only if it stays within 1%/)).toBeVisible();
  await page.getByLabel('Retopology').selectOption('none');
  await page.getByRole('checkbox', { name: /Set the real-world size/ }).check();
  await page.getByLabel('Length (mm)').fill('100');
  await page.getByRole('button', { name: /^Enhance \(/ }).click();
  await page.getByRole('button', { name: 'View the new version' }).click();
  await expect(page.getByText(/Version 2 · Enhanced · current/)).toBeVisible();
  await expect(page.getByText(/Enhanced from version 1, which is unchanged/)).toBeVisible();

  await page.getByRole('tab', { name: 'Quality' }).click();
  const report = page.getByLabel('Quality report');
  await expect(report.getByText('Known (Y = 100 mm)')).toBeVisible();
  await expect(report.getByText('Real-world scale')).toBeVisible(); // an applied step

  await page.getByRole('tab', { name: 'Export' }).click();
  // FBX is offered only where Blender is installed; then it must work too.
  const { items } = await (await request.get('/api/v1/exports/formats')).json();
  const fbx = items.find((f: { format: string }) => f.format === 'fbx')?.available === true;
  const formats = fbx ? ['GLB', 'STL', 'FBX'] : ['GLB', 'STL'];
  if (!fbx) await expect(page.getByRole('button', { name: 'Export as FBX' })).toBeDisabled();
  for (const format of formats) {
    await page.getByRole('button', { name: `Export as ${format}` }).click();
    await expect(page.getByRole('link', { name: `Download ${format}` })).toBeVisible();
  }
  const files = page.getByRole('list', { name: 'Exported files' });
  await expect(files.getByText('Re-opened and verified')).toHaveCount(formats.length);
  await expect(files.getByText(/× 100 millimetres/)).toBeVisible(); // STL, Y = 100 mm

  const glb = await request.get(
    (await page.getByRole('link', { name: 'Download GLB' }).getAttribute('href'))!,
  );
  expect((await glb.body()).subarray(0, 4).toString('ascii')).toBe('glTF');
  const stl = await request.get(
    (await page.getByRole('link', { name: 'Download STL' }).getAttribute('href'))!,
  );
  expect(stl.headers()['content-disposition']).toContain('-v2.stl');
  // Binary STL: 80-byte header, then the triangle count.
  expect((await stl.body()).readUInt32LE(80)).toBe(1280);
});
