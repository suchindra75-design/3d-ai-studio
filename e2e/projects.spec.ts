import { expect, test } from '@playwright/test';

import { texturedPng } from './fixtures';

test('landing page links into the studio', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('Photographs in.');
  await page.getByRole('link', { name: 'Open Studio' }).first().click();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible();
});

test('project lifecycle: create, upload, inspect, persist, delete', async ({ page }, testInfo) => {
  const name = `E2E vase ${testInfo.project.name} ${Date.now()}`;

  // Create
  await page.goto('/projects/new');
  await page.getByLabel('Name').fill(name);
  await page.getByRole('radio', { name: /Multiple photos/ }).click();
  await page.getByRole('button', { name: 'Create project' }).click();
  await expect(page.getByRole('heading', { name })).toBeVisible();
  await expect(page.getByText('Not enough photos').first()).toBeVisible();
  const projectUrl = page.url();

  // Upload two photos and one unsupported file through the real file input
  await page.locator('input[type=file]').setInputFiles([
    { name: 'front.png', mimeType: 'image/png', buffer: texturedPng(900, 700, 1) },
    { name: 'side.png', mimeType: 'image/png', buffer: texturedPng(900, 700, 2) },
    { name: 'readme.txt', mimeType: 'text/plain', buffer: Buffer.from('hello') },
  ]);
  const uploads = page.getByRole('region', { name: 'Uploads' });
  await expect(uploads.getByText('Uploaded and analysed')).toHaveCount(2);
  await expect(uploads.getByText('Only JPEG, PNG and WebP images are supported.')).toBeVisible();

  const photos = page.getByRole('list', { name: 'Photos' });
  await expect(photos.getByRole('button')).toHaveCount(2);

  // Capture assessment updates from upload-time analysis
  const capture = page.getByRole('region', { name: 'Capture assessment' });
  await expect(capture.getByText('enough for AI multi-view only')).toBeVisible();
  await expect(capture.getByText('Overlap not measured yet')).toBeVisible();

  // Inspect a photo
  await photos.getByRole('button', { name: /front\.png/ }).click();
  const details = page.getByRole('region', { name: 'Photo details' });
  await expect(details.getByText('Good quality')).toBeVisible();
  await expect(details.getByText('900 × 700')).toBeVisible();

  // Re-uploading identical bytes is detected as a duplicate
  await page
    .locator('input[type=file]')
    .setInputFiles([
      { name: 'front-again.png', mimeType: 'image/png', buffer: texturedPng(900, 700, 1) },
    ]);
  await expect(uploads.getByText('Already in this project — skipped')).toBeVisible();
  await expect(photos.getByRole('button')).toHaveCount(2);

  // Data survives a full reload (served from the database, not client state)
  await page.reload();
  await expect(page.getByRole('list', { name: 'Photos' }).getByRole('button')).toHaveCount(2);

  // Delete one photo
  await page
    .getByRole('list', { name: 'Photos' })
    .getByRole('button', { name: /side\.png/ })
    .click();
  await page
    .getByRole('region', { name: 'Photo details' })
    .getByRole('button', { name: 'Delete' })
    .click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete photo' }).click();
  await expect(page.getByRole('list', { name: 'Photos' }).getByRole('button')).toHaveCount(1);

  // The dashboard shows the project with its photo count
  await page.goto('/projects');
  const card = page.getByRole('link', { name: new RegExp(name) });
  await expect(card).toContainText('1 photo');

  // Delete the project
  await page.goto(projectUrl);
  await page.getByRole('button', { name: 'Project actions' }).click();
  await page.getByRole('menuitem', { name: /Delete project/ }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Delete project' }).click();
  await expect(page).toHaveURL(/\/projects$/);
  await expect(page.getByRole('link', { name: new RegExp(name) })).toHaveCount(0);
});

test('unknown project shows a not-found error', async ({ page }) => {
  await page.goto('/projects/00000000-0000-0000-0000-000000000000');
  const error = page.getByRole('alert').filter({ hasText: 'Project not found.' });
  await expect(error).toContainText('Not found');
});
