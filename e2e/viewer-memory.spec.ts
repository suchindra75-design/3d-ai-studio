import { execFileSync } from 'node:child_process';

import { expect, test, type Page } from '@playwright/test';

import { modelWithVersion } from './fixtures';

// Switching versions frees the previous one: GPU geometry and textures return to the
// same level each time, and (on a production build, playwright.production.config.ts)
// so does the JavaScript heap.

test.use({ launchOptions: { args: ['--js-flags=--expose-gc'] } });

function ball(subdivisions: number, seed: number): Buffer {
  const script = [
    'import sys, numpy as np, trimesh',
    'from PIL import Image',
    `ball = trimesh.creation.icosphere(subdivisions=${subdivisions}, radius=0.5)`,
    'ball.unmerge_vertices()',
    `rng = np.random.default_rng(${seed})`,
    'image = Image.fromarray(rng.integers(0, 255, (512, 512, 3), dtype=np.uint8))',
    'ball.visual = trimesh.visual.TextureVisuals(uv=rng.random((len(ball.vertices), 2)), image=image)',
    "sys.stdout.buffer.write(trimesh.Scene(ball).export(file_type='glb'))",
  ].join('\n');
  return execFileSync('.venv/bin/python', ['-I', '-c', script], { maxBuffer: 1 << 28 });
}

interface Usage {
  geometries: number;
  textures: number;
  heapMb: number;
}

async function usage(page: Page): Promise<Usage> {
  return page.evaluate(async () => {
    const probe = (
      window as unknown as {
        __ai3dViewerProbe: {
          views: Record<
            string,
            { get: () => { gl: { info: { memory: Record<string, number> } } } }
          >;
        };
      }
    ).__ai3dViewerProbe;
    // Let React finish unmounting, then collect garbage twice.
    await new Promise((r) => setTimeout(r, 300));
    (globalThis as unknown as { gc?: () => void }).gc?.();
    (globalThis as unknown as { gc?: () => void }).gc?.();
    const view = Object.values(probe.views)[0]!;
    const memory = view.get().gl.info.memory;
    return {
      geometries: memory.geometries!,
      textures: memory.textures!,
      heapMb:
        ((performance as unknown as { memory: { usedJSHeapSize: number } }).memory.usedJSHeapSize ??
          0) / 1e6,
    };
  });
}

test('switching versions does not keep the previous ones in memory', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'one run is enough');
  test.setTimeout(240_000);
  const { projectId, modelId } = await modelWithVersion(request, 'E2E switching', ball(6, 1));
  const v2 = (await (await request.get(`/api/v1/models/${modelId}`)).json()).current_version;
  const third = await request.post(`/api/v1/models/${modelId}/versions`, {
    multipart: {
      file: { name: 'v3.glb', mimeType: 'model/gltf-binary', buffer: ball(5, 2) },
      parent_version_id: v2.id,
    },
  });
  expect(third.status()).toBe(201);

  await page.addInitScript(() => {
    (window as unknown as { __ai3dViewerProbe: object }).__ai3dViewerProbe = { views: {} };
  });
  await page.goto(`/projects/${projectId}/models/${modelId}`);
  const canvasOf = (n: number) =>
    page.getByRole('img', { name: new RegExp(`3D view of .*version ${n}$`) });
  await expect(canvasOf(3)).toBeVisible();
  await page.getByRole('tab', { name: /^Versions/ }).click();
  const history = page.getByRole('list', { name: 'Version history' });
  const view = async (n: number) => {
    await history
      .getByRole('listitem')
      .filter({ has: page.getByText(`Version ${n}`, { exact: true }) })
      .getByRole('button', { name: 'View' })
      .click();
    await expect(canvasOf(n)).toBeVisible();
    await page.getByRole('tab', { name: /^Versions/ }).click();
  };

  // Version 2 (a 82K-triangle textured model), then away and back, several times.
  await view(2);
  const first = await usage(page);
  const samples: Usage[] = [];
  for (let round = 0; round < 4; round += 1) {
    await view(3);
    await view(1);
    await view(2);
    samples.push(await usage(page));
  }
  testInfo.annotations.push({
    type: 'GPU geometries/textures and JS heap (MB) on version 2, first and after each round',
    description: JSON.stringify([first, ...samples]),
  });
  for (const sample of samples) {
    expect(sample.geometries).toBe(first.geometries);
    expect(sample.textures).toBe(first.textures);
  }
  // The heap may move a little (caches), but not grow by a model (about 6.6 MB) each
  // round. Only checked on a production build: development mode keeps old components
  // for its tools, which grows the heap by about that much per round (measured).
  if (process.env.AI3D_E2E_PRODUCTION === '1') {
    expect(samples[samples.length - 1]!.heapMb).toBeLessThan(first.heapMb + 5);
  }
});
