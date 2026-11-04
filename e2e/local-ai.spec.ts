import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

import { expect, test } from '@playwright/test';

// The real local AI engine (TripoSR) on this machine: needs AI3D_LOCAL_AI_DIR
// pointing at an installed engine (`uv run ai3d-local-ai install`). The photo is
// the real phone photo that ships with TripoSR's examples (MIT) in the engine.
const engineDir = process.env.AI3D_LOCAL_AI_DIR;
test.skip(!engineDir, 'set AI3D_LOCAL_AI_DIR to run local AI generation');

function realPhoto(): Buffer {
  const src = join(engineDir!, 'src');
  const triposr = readdirSync(src).find((name) => name.startsWith('TripoSR-'))!;
  return readFileSync(join(src, triposr, 'examples', 'captured.jpeg'));
}

test('one real photo becomes a 3D model on this computer', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one real generation is enough');
  test.setTimeout(400_000);

  const project = await (
    await request.post('/api/v1/projects', {
      data: { name: 'E2E local AI', input_mode: 'single_image', default_quality_mode: 'fast' },
    })
  ).json();
  const upload = await request.post(`/api/v1/projects/${project.id}/images`, {
    multipart: { files: { name: 'plush.jpg', mimeType: 'image/jpeg', buffer: realPhoto() } },
  });
  expect((await upload.json()).results[0].status).toBe('created');

  await page.goto(`/projects/${project.id}`);
  await page.getByLabel('Processing', { exact: true }).selectOption({ label: 'Local' });
  await expect(page.getByText('Local AI (TripoSR)').first()).toBeVisible();
  await expect(page.getByText('Estimated cost: Free')).toBeVisible();
  // Nothing leaves the computer, so there is nothing to consent to.
  await expect(page.getByRole('checkbox', { name: /Send 1 photo/ })).toHaveCount(0);
  await page.getByRole('button', { name: 'Generate', exact: true }).click();

  const jobs = page.getByRole('region', { name: 'Generations' });
  await expect(jobs.getByText('Running', { exact: true })).toBeVisible();
  await expect(jobs.getByText('Done', { exact: true })).toBeVisible({ timeout: 360_000 });

  const models = page.getByRole('region', { name: '3D models' });
  await expect(models.getByText('Local AI (TripoSR) · Fast')).toBeVisible();
  await expect(models.getByText('AI-generated: unseen surfaces invented')).toBeVisible();

  await page.getByRole('link', { name: /Open .* in 3D/ }).click();
  await expect(page.getByRole('img', { name: /3D view of .*version 1/ })).toBeVisible();
  await expect(page.getByText('3D view unavailable')).toHaveCount(0);
  const inspector = page.getByRole('complementary', { name: 'Inspector' });
  await expect(inspector.getByText('AI-generated: unseen surfaces invented')).toBeVisible();
  await page.waitForTimeout(1500); // let the viewer settle before the screenshot
  await page.screenshot({ path: testInfo.outputPath('local-ai-workspace.png') });
});
