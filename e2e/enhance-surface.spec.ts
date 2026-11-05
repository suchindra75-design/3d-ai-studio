import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

import { modelWithVersion } from './fixtures';

// Phase 13's surface tools run from the Enhance tab, checked in the saved file:
// a bumpy sphere with vertex colours (orange top, blue bottom).

const PY = '.venv/bin/python';

function bumpySphere(): Buffer {
  const script = [
    'import sys, numpy as np, trimesh',
    'rng = np.random.default_rng(3)',
    'ball = trimesh.creation.icosphere(subdivisions=4, radius=0.5)',
    'v = ball.vertices',
    'ball.vertices = v * (1 + 0.04 * rng.standard_normal(len(v)))[:, None]',
    'top = ball.vertices[:, 1] > 0',
    'colours = np.where(top[:, None], [230, 120, 20, 255], [30, 60, 220, 255]).astype(np.uint8)',
    'ball.visual = trimesh.visual.ColorVisuals(ball, vertex_colors=colours)',
    "scene = trimesh.Scene(); scene.add_geometry(ball, geom_name='Ball', node_name='Ball')",
    "sys.stdout.buffer.write(scene.export(file_type='glb'))",
  ].join('\n');
  return execFileSync(PY, ['-I', '-c', script]);
}

interface Facts {
  triangles: number;
  roughness: number;
  volume: number;
  top: number[];
  bottom: number[];
  textures: string[];
}

/** Facts about a saved GLB, read as the app reads files. */
function inspect(glb: Buffer): Facts {
  const folder = mkdtempSync(join(tmpdir(), 'ai3d-surface-'));
  const path = join(folder, 'saved.glb');
  writeFileSync(path, glb);
  const script = [
    'import sys, json, struct, numpy as np, trimesh',
    'from pathlib import Path',
    'from ai3d.processing.mesh import load_model_scene',
    'raw = Path(sys.argv[1]).read_bytes()',
    'n = struct.unpack("<I", raw[12:16])[0]',
    'gltf = json.loads(raw[20:20 + n])',
    'textures = sorted({k for m in gltf.get("materials", []) for k in m if k.endswith("Texture")}',
    '  | {k for m in gltf.get("materials", []) for k in m.get("pbrMetallicRoughness", {}) if k.endswith("Texture")})',
    'scene = load_model_scene(Path(sys.argv[1]))',
    'mesh = scene.to_geometry() if hasattr(scene, "to_geometry") else scene.dump(concatenate=True)',
    'c = mesh.vertices.mean(axis=0)',
    'r = np.linalg.norm(mesh.vertices - c, axis=1)',
    'meshes = list(scene.geometry.values())',
    '# vertex colours: on their own, or beside textures (baked maps) as a vertex attribute',
    'def rgb(m): return np.asarray(m.visual.vertex_colors if m.visual.kind == "vertex" else m.visual.vertex_attributes["color"])[:, :3]',
    'colours = np.concatenate([rgb(m) for m in meshes])',
    'ys = np.concatenate([m.vertices[:, 1] for m in meshes]) - c[1]',
    'print(json.dumps({"triangles": int(len(mesh.faces)), "roughness": float(r.std() / r.mean()),',
    '  "volume": float(abs(mesh.volume)), "top": colours[ys > 0.25].mean(axis=0).tolist(),',
    '  "bottom": colours[ys < -0.25].mean(axis=0).tolist(), "textures": textures}))',
  ].join('\n');
  return JSON.parse(execFileSync(PY, ['-I', '-c', script, path]).toString()) as Facts;
}

async function current(request: APIRequestContext, modelId: string) {
  const versions = await (await request.get(`/api/v1/models/${modelId}/versions`)).json();
  const latest = versions.items[0];
  return { latest, facts: inspect(await (await request.get(latest.file_url)).body()) };
}

async function enhance(page: Page, version: number) {
  await page.getByRole('button', { name: /^Enhance \(/ }).click();
  await page.getByRole('button', { name: 'View the new version' }).click({ timeout: 300_000 });
  await expect(page.getByText(new RegExp(`Version ${version} · Enhanced · current`))).toBeVisible();
}

function expectOrangeTopBlueBottom(facts: Facts) {
  const [r1, , b1] = facts.top;
  const [r2, , b2] = facts.bottom;
  expect(r1!).toBeGreaterThan(b1! + 100); // orange
  expect(b2!).toBeGreaterThan(r2! + 100); // blue
}

test('smoothing from the Enhance tab smooths the saved surface and keeps its colours', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one run is enough');
  test.setTimeout(180_000);
  const { projectId, modelId } = await modelWithVersion(request, 'E2E smoothing', bumpySphere());
  const before = (await current(request, modelId)).facts;
  expect(before.roughness).toBeGreaterThan(0.03);

  await page.goto(`/projects/${projectId}/models/${modelId}`);
  await expect(page.getByRole('img', { name: /3D view of .*version 2/ })).toBeVisible();
  await page.getByRole('tab', { name: 'Enhance' }).click();
  await page.getByLabel('Smoothing', { exact: true }).selectOption('10');
  await expect(
    page.getByRole('list', { name: 'Enhancement steps' }).getByText('Smoothing'),
  ).toBeVisible();
  await enhance(page, 3);

  const { latest, facts } = await current(request, modelId);
  expect(latest.notes).toContain('Smoothing');
  expect(latest.quality_report.status).not.toBe('fail');
  expect(facts.roughness).toBeLessThan(before.roughness * 0.5); // bumps flattened
  expect(Math.abs(facts.volume / before.volume - 1)).toBeLessThan(0.05); // volume kept
  expectOrangeTopBlueBottom(facts);
});

test('QuadriFlow retopology with normal and occlusion maps, from the Enhance tab', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one run is enough');
  const { items } = await (await request.get('/api/v1/exports/formats')).json();
  const blender = items.find((f: { format: string }) => f.format === 'fbx')?.available === true;
  test.skip(!blender, 'needs Blender');
  test.setTimeout(420_000);
  const { projectId, modelId } = await modelWithVersion(request, 'E2E retopology', bumpySphere());
  const before = (await current(request, modelId)).facts;

  await page.goto(`/projects/${projectId}/models/${modelId}`);
  await expect(page.getByRole('img', { name: /3D view of .*version 2/ })).toBeVisible();
  await page.getByRole('tab', { name: 'Enhance' }).click();
  await page.getByLabel('Retopology', { exact: true }).selectOption('quadriflow');
  const quads = page.getByLabel('About how many quads');

  // Too few quads for these bumps: not kept, and the panel says why.
  await quads.fill('300');
  await page.getByRole('button', { name: /^Enhance \(/ }).click();
  await expect(page.getByText('No new version was created.')).toBeVisible({ timeout: 300_000 });
  await expect(page.getByRole('list', { name: 'Not applied' })).toContainText(
    /Retopology \(QuadriFlow, about 300 quads\): Not kept: .* more than the 1% allowed/,
  );

  // Enough quads: kept, with both maps.
  await quads.fill('5000');
  await page.getByRole('checkbox', { name: /Bake a normal map/ }).check();
  await page.getByRole('checkbox', { name: /Bake ambient occlusion/ }).check();
  await enhance(page, 3);

  const { latest, facts } = await current(request, modelId);
  expect(latest.notes).toContain('Retopology (QuadriFlow, about 5,000 quads)');
  expect(latest.notes).toContain('Normal / occlusion maps');
  expect(latest.quality_report.status).not.toBe('fail');
  // About 5,000 quads, each saved as two triangles (QuadriFlow lands near the target).
  expect(facts.triangles).toBeGreaterThan(6000);
  expect(facts.triangles).toBeLessThan(14000);
  expect(facts.triangles).not.toBe(before.triangles);
  expect(facts.textures).toEqual(expect.arrayContaining(['normalTexture', 'occlusionTexture']));
  expect(Math.abs(facts.volume / before.volume - 1)).toBeLessThan(0.05); // same shape
  expectOrangeTopBlueBottom(facts); // colours carried over
});
