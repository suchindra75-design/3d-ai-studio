import { execFileSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { expect, test } from '@playwright/test';

// Real COLMAP on this machine: needs AI3D_COLMAP_PATH (COLMAP 4). The photos are
// rendered by the backend test fixture (a textured box seen from 30 angles).
test.skip(!process.env.AI3D_COLMAP_PATH, 'set AI3D_COLMAP_PATH to run local photogrammetry');

function renderCaptureSet(): { name: string; data: Buffer }[] {
  const dir = mkdtempSync(join(tmpdir(), 'ai3d-capture-'));
  try {
    execFileSync(
      '../.venv/bin/python',
      [
        '-c',
        'import sys; from pathlib import Path; from tests import scenes\n' +
          'for name, data in scenes.photo_set(): Path(sys.argv[1], name).write_bytes(data)',
        dir,
      ],
      { cwd: 'backend', stdio: 'pipe', env: { ...process.env, PYTHONPATH: '.' } },
    );
    return readdirSync(dir)
      .sort()
      .map((name) => ({ name, data: readFileSync(join(dir, name)) }));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test('reconstruct a model from 30 photos on this computer', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one full reconstruction is enough');
  test.setTimeout(300_000);

  const project = await (
    await request.post('/api/v1/projects', {
      data: { name: 'E2E photogrammetry', input_mode: 'multi_view' },
    })
  ).json();
  const form = new FormData();
  for (const photo of renderCaptureSet()) {
    form.append(
      'files',
      new Blob([new Uint8Array(photo.data)], { type: 'image/jpeg' }),
      photo.name,
    );
  }
  const upload = await request.post(`/api/v1/projects/${project.id}/images`, { multipart: form });
  expect((await upload.json()).results).toHaveLength(30);

  await page.goto(`/projects/${project.id}`);

  // Overlap measured on request with real feature matching.
  await page.getByRole('button', { name: 'Measure overlap' }).click();
  await expect(page.getByText('Photos overlap well')).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText(/All 30 photos connect into one set/)).toBeVisible();

  // Photogrammetry is the default for a capture set; nothing is uploaded, nothing to consent to.
  await expect(page.getByRole('radio', { name: /Photogrammetry/ })).toHaveAttribute(
    'aria-checked',
    'true',
  );
  await expect(page.getByText('Estimated cost: Free')).toBeVisible();
  await expect(page.getByRole('checkbox', { name: /^Send/ })).toHaveCount(0);
  await page.getByRole('button', { name: 'Reconstruct', exact: true }).click();

  const jobs = page.getByRole('region', { name: 'Generations' });
  await expect(jobs.getByText(/Local photogrammetry \(COLMAP\)/)).toBeVisible();
  await expect(jobs.getByText('Done', { exact: true })).toBeVisible({ timeout: 240_000 });

  const models = page.getByRole('region', { name: '3D models' });
  await expect(models.getByText('Photogrammetry · 30 photos')).toBeVisible();
  await expect(models.getByText('Measured from photos')).toBeVisible();
  await expect(models.getByText(/from 30 of 30 photos/)).toBeVisible();

  await models.getByRole('link', { name: /Open .* in 3D/ }).click();
  await expect(page.getByRole('img', { name: /3D view of Photogrammetry/ })).toBeVisible();
  const inspector = page.getByRole('complementary', { name: 'Inspector' });
  await expect(inspector.getByText(/scale is unknown/)).toBeVisible();
  await expect(inspector.getByText('1 / 1')).toBeVisible(); // one material, one texture
});
