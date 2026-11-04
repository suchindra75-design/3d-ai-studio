import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

import { expect, test } from '@playwright/test';

// Import an existing STL into a project, back the project up, and restore it.
function bracketStl(): Buffer {
  const script = [
    'import sys, trimesh',
    "sys.stdout.buffer.write(trimesh.creation.box(extents=(20, 30, 50)).export(file_type='stl'))",
  ].join('\n');
  return execFileSync('.venv/bin/python', ['-I', '-c', script]);
}

test('import a 3D file, back the project up and restore it', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one round trip is enough');
  const project = await (
    await request.post('/api/v1/projects', {
      data: { name: 'E2E transfer', input_mode: 'single_image' },
    })
  ).json();
  await page.goto(`/projects/${project.id}`);

  await page.getByRole('button', { name: 'Import 3D file' }).click();
  const dialog = page.getByRole('dialog', { name: 'Import a 3D file' });
  await dialog
    .getByLabel('File', { exact: true })
    .setInputFiles({ name: 'bracket.stl', mimeType: 'model/stl', buffer: bracketStl() });
  await dialog.getByLabel('The file’s size is the real object’s size').check();
  await dialog.getByRole('button', { name: 'Import', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText('bracket', { exact: true })).toBeVisible();

  const models = await (await request.get(`/api/v1/projects/${project.id}/models`)).json();
  const version = models.items[0].current_version;
  expect(version.source).toBe('imported');
  expect(version.quality_report.metrics.bbox_size[1]).toBeCloseTo(0.05, 6); // 50 mm tall, Y up
  expect(version.stats.scale.length_mm).toBeCloseTo(50, 3);

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('link', { name: 'Back up this project as a ZIP' }).click(),
  ]);
  expect(download.suggestedFilename()).toBe('e2e-transfer-backup.zip');
  const backup = readFileSync((await download.path())!);
  expect(backup.subarray(0, 2).toString('ascii')).toBe('PK');

  await page.goto('/projects');
  await page
    .getByLabel('Backup ZIP to restore')
    .setInputFiles({ name: 'backup.zip', mimeType: 'application/zip', buffer: backup });
  await expect(page.getByRole('heading', { name: 'E2E transfer (restored)' })).toBeVisible();
  await expect(page.getByText('bracket', { exact: true })).toBeVisible();
});
