import { expect, test, type Page } from '@playwright/test';

import { texturedPng } from '../fixtures';

// Run with `pnpm test:e2e:workflows`. The backend's mock provider fails the first
// task for each set of photos with a provider error and takes ~12 s per task.
const MOCK = 'Mock provider (tests only)';

async function projectWithPhoto(page: Page, name: string, seed: number) {
  await page.goto('/#/projects/new');
  await page.getByLabel('Name').fill(name);
  await page.getByRole('radio', { name: /Single photo/ }).click();
  await page.getByRole('switch', { name: /Allow sending this project/ }).click();
  await page.getByRole('button', { name: 'Create project' }).click();
  await expect(page.getByRole('heading', { name })).toBeVisible();
  await page
    .locator('input[type=file]')
    .setInputFiles([
      { name: 'vase.png', mimeType: 'image/png', buffer: texturedPng(800, 600, seed) },
    ]);
  await expect(
    page.getByRole('region', { name: 'Uploads' }).getByText('Uploaded and analysed'),
  ).toBeVisible();
}

async function generate(page: Page) {
  await page.getByLabel('Provider', { exact: true }).selectOption({ label: MOCK });
  await page.getByRole('checkbox', { name: /Send 1 photo to Mock provider/ }).check();
  await page.getByRole('button', { name: 'Generate', exact: true }).click();
}

test('a provider error is shown and retrying the job succeeds', async ({ page }) => {
  await projectWithPhoto(page, `Workflow error ${Date.now()}`, 21);
  await generate(page);

  // The job cards only (a failed job can list suggestions of its own).
  const jobs = page
    .getByRole('region', { name: 'Generations' })
    .getByRole('list')
    .first()
    .locator(':scope > li');
  await expect(jobs.first().getByText('Running')).toBeVisible();
  await expect(jobs.first().getByText('Failed', { exact: true })).toBeVisible();
  await expect(jobs.first().getByRole('alert')).toHaveText('Mock provider simulated task_failed.');
  await expect(page.getByRole('region', { name: '3D models' })).toHaveCount(0);

  // Retry asks first (photos go to the provider again), then runs as a new job.
  await jobs.first().getByRole('button', { name: 'Retry' }).click();
  await expect(page.getByText(/sends 1 photo to Mock provider \(tests only\) again/)).toBeVisible();
  await page.getByRole('button', { name: 'Run again' }).click();
  await expect(jobs).toHaveCount(2);
  await expect(jobs.first().getByText('Done', { exact: true })).toBeVisible();
  await expect(jobs.nth(1).getByText('Failed', { exact: true })).toBeVisible();
  const models = page.getByRole('region', { name: '3D models' });
  await expect(models.getByText('File checks passed')).toBeVisible();

  // The history survives a reload (it comes from the database).
  await page.reload();
  await expect(jobs).toHaveCount(2);
  await expect(jobs.nth(1).getByRole('alert')).toHaveText('Mock provider simulated task_failed.');
});

test('a running job can be cancelled and then retried', async ({ page }) => {
  await projectWithPhoto(page, `Workflow cancel ${Date.now()}`, 22);
  await generate(page);

  // The job cards only (a failed job can list suggestions of its own).
  const jobs = page
    .getByRole('region', { name: 'Generations' })
    .getByRole('list')
    .first()
    .locator(':scope > li');
  await expect(jobs.first().getByText('Running', { exact: true })).toBeVisible();
  await expect(
    jobs.first().getByRole('progressbar', { name: 'Generation progress' }),
  ).toBeVisible();
  await jobs.first().getByRole('button', { name: 'Cancel' }).click();
  await expect(jobs.first().getByText('Cancelled', { exact: true })).toBeVisible();
  await expect(jobs.first().getByRole('button', { name: 'Cancel' })).toHaveCount(0);

  // The cancelled task counted as this photo's first task, so the retry succeeds.
  await jobs.first().getByRole('button', { name: 'Retry' }).click();
  await page.getByRole('button', { name: 'Run again' }).click();
  await expect(jobs).toHaveCount(2);
  await expect(jobs.first().getByText('Done', { exact: true })).toBeVisible();
  await expect(jobs.nth(1).getByText('Cancelled', { exact: true })).toBeVisible();
});
