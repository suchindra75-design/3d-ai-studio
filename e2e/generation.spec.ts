import { expect, test, type APIRequestContext } from '@playwright/test';

import { texturedPng } from './fixtures';

// The e2e backend runs with AI3D_ENVIRONMENT=test, which registers the mock
// provider: it simulates a cloud provider and returns a placeholder sphere.
// Nothing in these tests contacts a real AI service.
const MOCK = 'Mock provider (tests only)';

async function projectWithPhoto(request: APIRequestContext, name: string) {
  const project = await (
    await request.post('/api/v1/projects', { data: { name, input_mode: 'single_image' } })
  ).json();
  const upload = await request.post(`/api/v1/projects/${project.id}/images`, {
    multipart: {
      files: { name: 'mug.png', mimeType: 'image/png', buffer: texturedPng(800, 600, 7) },
    },
  });
  const imageId = (await upload.json()).results[0].image.id as string;
  return { projectId: project.id as string, imageId };
}

test('generate a model with explicit consent, inspect it, delete it', async ({
  page,
  request,
}, testInfo) => {
  const { projectId } = await projectWithPhoto(request, `E2E mug ${testInfo.project.name}`);
  await page.goto(`/projects/${projectId}`);

  await page.getByLabel('Provider', { exact: true }).selectOption({ label: MOCK });
  await expect(page.getByText('Estimated cost: Free')).toBeVisible();
  await expect(page.getByText(/sides the camera did not see are invented/)).toBeVisible();

  const generate = page.getByRole('button', { name: 'Generate', exact: true });
  await expect(generate).toBeDisabled();
  await page.getByRole('checkbox', { name: /Send 1 photo to Mock provider/ }).check();
  await expect(generate).toBeDisabled(); // the project still keeps photos local
  await page.getByRole('checkbox', { name: /Allow it to use cloud providers/ }).check();
  await generate.click();

  const jobs = page.getByRole('region', { name: 'Generations' });
  await expect(jobs.getByText('Done', { exact: true })).toBeVisible({ timeout: 45_000 });
  await expect(page.getByText('Cloud upload allowed')).toBeVisible();

  await jobs.getByRole('button', { name: 'Details' }).click();
  await expect(
    jobs.getByText(/without metadata \(location, camera details removed\)/),
  ).toBeVisible();

  const models = page.getByRole('region', { name: '3D models' });
  await expect(models.getByText(`${MOCK} · Fast`)).toBeVisible();
  await expect(models.getByText('AI-generated: unseen surfaces invented')).toBeVisible();
  await expect(models.getByText('File checks passed')).toBeVisible();
  await expect(models.getByText(/not a reconstruction of your photos/)).toBeVisible();
  await expect(models.getByRole('img', { name: /Preview of/ })).toBeVisible();

  // The download is a real GLB file.
  const href = await models.getByRole('link', { name: 'GLB' }).getAttribute('href');
  const file = await request.get(href!);
  expect(file.headers()['content-type']).toBe('model/gltf-binary');
  expect((await file.body()).subarray(0, 4).toString('ascii')).toBe('glTF');

  // Survives a reload, then delete it.
  await page.reload();
  await models.getByRole('button', { name: /^Delete / }).click();
  await page.getByRole('button', { name: 'Delete model' }).click();
  await expect(models).toHaveCount(0);
  await expect(jobs.getByText('Done', { exact: true })).toBeVisible();
});

test('a job waiting for permission uploads nothing and can be declined', async ({
  page,
  request,
}, testInfo) => {
  const { projectId, imageId } = await projectWithPhoto(
    request,
    `E2E consent ${testInfo.project.name}`,
  );
  const job = await (
    await request.post(`/api/v1/projects/${projectId}/generations`, {
      data: { inputs: [{ image_id: imageId }], provider: 'mock' },
    })
  ).json();
  expect(job.status).toBe('waiting_for_consent');

  await page.goto(`/projects/${projectId}`);
  const jobs = page.getByRole('region', { name: 'Generations' });
  await expect(jobs.getByText('Waiting for permission')).toBeVisible();
  await expect(jobs.getByText(/Nothing has been uploaded yet/)).toBeVisible();
  await expect(jobs.getByRole('button', { name: 'Allow and start' })).toBeDisabled();

  // Live events reach the browser through the web app's proxy while the job is
  // still open (an SSE stream that was buffered would deliver nothing here).
  const firstEvent = await page.evaluate(
    (url) =>
      new Promise<string>((resolve, reject) => {
        const source = new EventSource(url);
        const timer = setTimeout(() => {
          source.close();
          reject(new Error('no event within 5 s'));
        }, 5_000);
        source.addEventListener('status', (event) => {
          clearTimeout(timer);
          source.close();
          resolve(JSON.parse((event as MessageEvent).data).message);
        });
      }),
    `/api/v1/jobs/${job.id}/stream`,
  );
  expect(firstEvent).toContain('Waiting for your permission');

  await jobs.getByRole('button', { name: 'Don’t send' }).click();
  await expect(jobs.getByText('Cancelled', { exact: true })).toBeVisible();
  const events = await (await request.get(`/api/v1/jobs/${job.id}/events`)).json();
  expect(events.items.map((e: { message: string }) => e.message)).toContain(
    'Cancelled before anything was sent.',
  );
});

test('settings list the providers without exposing keys', async ({ page }) => {
  await page.goto('/settings');
  await expect(page.getByText('AI providers', { exact: true })).toBeVisible();
  await expect(page.getByText('Tripo AI', { exact: true })).toBeVisible();
  await expect(page.getByText('Meshy AI', { exact: true })).toBeVisible();
  await expect(page.getByText('Test provider', { exact: true })).toBeVisible();
  await expect(page.getByText('AI3D_TRIPO_API_KEY')).toBeVisible();
});
