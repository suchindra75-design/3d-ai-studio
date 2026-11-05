import { execFileSync } from 'node:child_process';
import { mkdtempSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { expect, test } from '@playwright/test';

import { modelWithVersion } from './fixtures';

// The real local AI engine (TripoSR) on this machine: needs AI3D_LOCAL_AI_DIR
// pointing at an installed engine (`uv run ai3d-local-ai install`). The photo is
// the real phone photo that ships with TripoSR's examples (MIT) in the engine.
const engineDir = process.env.AI3D_LOCAL_AI_DIR;
test.skip(!engineDir, 'set AI3D_LOCAL_AI_DIR to run local AI generation');

function realPhotoPath(): string {
  const src = join(engineDir!, 'src');
  const triposr = readdirSync(src).find((name) => name.startsWith('TripoSR-'))!;
  return join(src, triposr, 'examples', 'captured.jpeg');
}

function realPhoto(): Buffer {
  return readFileSync(realPhotoPath());
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

const PY = '.venv/bin/python';

/** A box textured with a 256 px patch of the real photo (its most detailed part). */
function photoTexturedBox(): Buffer {
  const script = [
    'import sys, numpy as np, trimesh',
    'from PIL import Image',
    'photo = Image.open(sys.argv[1]).convert("RGB")',
    'grey = np.asarray(photo.convert("L"), float)',
    'best = max((np.abs(np.diff(grey[y:y+256, x:x+256], axis=0)).mean(), x, y)',
    '  for y in range(0, photo.height - 256, 64) for x in range(0, photo.width - 256, 64))',
    'texture = photo.crop((best[1], best[2], best[1] + 256, best[2] + 256))',
    'box = trimesh.creation.box(extents=(1, 1, 1))',
    'box.unmerge_vertices()',
    'uv = np.random.default_rng(4).random((len(box.vertices), 2))',
    'box.visual = trimesh.visual.TextureVisuals(uv=uv, image=texture)',
    "sys.stdout.buffer.write(trimesh.Scene(box).export(file_type='glb'))",
  ].join('\n');
  return execFileSync(PY, ['-I', '-c', script, realPhotoPath()]);
}

/** Size of the base-colour texture in a saved GLB, read as the app reads files. */
function textureSize(glb: Buffer): number[] {
  const path = join(mkdtempSync(join(tmpdir(), 'ai3d-upscale-')), 'saved.glb');
  writeFileSync(path, glb);
  const script = [
    'import sys, json',
    'from pathlib import Path',
    'from ai3d.processing.mesh import load_model_scene',
    'scene = load_model_scene(Path(sys.argv[1]))',
    '(mesh,) = scene.geometry.values()',
    'print(json.dumps(list(mesh.visual.material.baseColorTexture.size)))',
  ].join('\n');
  return JSON.parse(execFileSync(PY, ['-I', '-c', script, path]).toString()) as number[];
}

test('AI texture upscaling from the Enhance tab, on this computer', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one real run is enough');
  test.setTimeout(300_000);
  const { projectId, modelId } = await modelWithVersion(
    request,
    'E2E upscaling',
    photoTexturedBox(),
  );
  const versions = async () =>
    (await (await request.get(`/api/v1/models/${modelId}/versions`)).json()).items;
  const before = (await versions())[0];
  expect(textureSize(await (await request.get(before.file_url)).body())).toEqual([256, 256]);

  await page.goto(`/projects/${projectId}/models/${modelId}`);
  await expect(page.getByRole('img', { name: /3D view of .*version 2/ })).toBeVisible();
  await page.getByRole('tab', { name: 'Enhance' }).click();
  await page.getByLabel('AI texture upscaling').selectOption('2');
  await expect(page.getByText(/does not recover detail the photos never had/)).toBeVisible();
  await page.getByRole('button', { name: /^Enhance \(/ }).click();
  await page.getByRole('button', { name: 'View the new version' }).click({ timeout: 240_000 });
  await expect(page.getByText(/Version 3 · Enhanced · current/)).toBeVisible();

  const after = (await versions())[0];
  expect(after.notes).toContain('AI texture upscaling (×2)');
  const steps = after.quality_report.metrics.enhancement as { code: string; detail: string }[];
  const upscale = steps.find((s) => s.code === 'upscale')!;
  expect(upscale.detail).toContain('256×256 → 512×512');
  expect(upscale.detail).toContain('does not recover detail');
  expect(textureSize(await (await request.get(after.file_url)).body())).toEqual([512, 512]);
});
