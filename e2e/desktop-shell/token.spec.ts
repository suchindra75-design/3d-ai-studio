import { expect, request as apiRequest, test } from '@playwright/test';

import { BACKEND_PORT, TEST_TOKEN } from '../../playwright.desktop.config';
import { texturedPng } from '../fixtures';

// Run with `pnpm test:e2e:desktop`. Uses the test-only mock provider.
const BACKEND = `http://127.0.0.1:${BACKEND_PORT}`;

test('the backend refuses callers without the token', async () => {
  const anonymous = await apiRequest.newContext({ baseURL: BACKEND });
  expect((await anonymous.get('/api/v1/health')).status()).toBe(200);
  const refused = await anonymous.get('/api/v1/projects');
  expect(refused.status()).toBe(401);
  expect((await refused.json()).error.code).toBe('unauthorized');
  const wrong = await anonymous.get('/api/v1/projects', {
    headers: { Authorization: 'Bearer not-the-token-at-all' },
  });
  expect(wrong.status()).toBe(401);
  await anonymous.dispose();
});

test('the desktop UI works end to end through the token', async ({ page }) => {
  const api = await apiRequest.newContext({
    baseURL: BACKEND,
    extraHTTPHeaders: { Authorization: `Bearer ${TEST_TOKEN}` },
  });
  const refused: string[] = [];
  page.on('response', (r) => {
    if (r.url().startsWith(BACKEND) && r.status() === 401) refused.push(r.url());
  });

  // Create a project in the UI (fetch with the Authorization header).
  const name = `Desktop E2E ${Date.now()}`;
  await page.goto('/');
  await expect(page).toHaveURL(/#\/projects$/);
  await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible();
  await page.goto('/#/projects/new');
  await page.getByLabel('Name').fill(name);
  await page.getByRole('radio', { name: /Single photo/ }).click();
  // The mock provider counts as a cloud provider, so the project must allow uploads.
  await page.getByRole('switch', { name: /Allow sending this project/ }).click();
  await page.getByRole('button', { name: 'Create project' }).click();
  await expect(page.getByRole('heading', { name })).toBeVisible();
  const projectId = /#\/projects\/([^/]+)$/.exec(page.url())![1]!;

  // Upload (XHR with the header) and show the thumbnail (<img> with the token in its URL).
  await page
    .locator('input[type=file]')
    .setInputFiles([{ name: 'mug.png', mimeType: 'image/png', buffer: texturedPng(800, 600, 11) }]);
  await expect(
    page.getByRole('region', { name: 'Uploads' }).getByText('Uploaded and analysed'),
  ).toBeVisible();
  const thumbnail = page.getByRole('list', { name: 'Photos' }).locator('img').first();
  await expect(thumbnail).toHaveAttribute('src', /access_token=/);
  await expect
    .poll(() => thumbnail.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth))
    .toBeGreaterThan(0);

  // A placeholder model from the mock provider, then the 3D workspace loads its GLB.
  const images = await (await api.get(`/api/v1/projects/${projectId}/images`)).json();
  const job = await (
    await api.post(`/api/v1/projects/${projectId}/generations`, {
      data: {
        inputs: [{ image_id: images.items[0].id }],
        provider: 'mock',
        quality_mode: 'high_quality',
        cloud_upload_consent: true,
      },
    })
  ).json();
  await expect
    .poll(async () => (await (await api.get(`/api/v1/jobs/${job.id}`)).json()).status, {
      timeout: 45_000,
    })
    .toBe('succeeded');
  await page.reload();
  // In a plain browser downloads are links carrying the token; they work.
  const download = page.getByRole('link', { name: /Download .* as GLB/ }).first();
  const href = await download.getAttribute('href');
  expect(href).toMatch(/access_token=/);
  const file = await page.request.get(href!);
  expect(file.status()).toBe(200);
  expect((await file.body()).subarray(0, 4).toString('ascii')).toBe('glTF');

  await page.getByRole('link', { name: /Open .* in 3D/ }).click();
  await expect(page.getByRole('img', { name: /3D view of .*version 1/ })).toBeVisible();
  const inspector = page.getByRole('complementary', { name: 'Inspector' });
  await expect(inspector.getByText('1,280', { exact: true })).toBeVisible(); // triangles

  expect(refused).toEqual([]);
  await api.dispose();
});
