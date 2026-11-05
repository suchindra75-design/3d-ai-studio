import { deflateSync } from 'node:zlib';

import { expect, type APIRequestContext } from '@playwright/test';

/** Minimal PNG encoder for synthetic test photos (no binary fixtures in the repo). */
function crc32(buffer: Buffer): number {
  let crc = ~0;
  for (const byte of buffer) {
    crc ^= byte;
    for (let k = 0; k < 8; k += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return ~crc >>> 0;
}

function chunk(type: string, data: Buffer): Buffer {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([length, body, crc]);
}

/** A textured (sharp, feature-rich) RGB image. `blur` averages blocks to make it soft. */
export function texturedPng(width = 900, height = 700, seed = 1, block = 6): Buffer {
  let state = seed * 2654435761;
  const random = () => (state = (state * 1103515245 + 12345) >>> 0) / 2 ** 32;
  const cols = Math.ceil(width / block);
  const colors = Array.from({ length: Math.ceil(height / block) * cols }, () => [
    random() * 255,
    random() * 200,
    random() * 150,
  ]);
  const raw = Buffer.alloc((width * 3 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    raw[y * (width * 3 + 1)] = 0; // filter: none
    for (let x = 0; x < width; x += 1) {
      const c = colors[Math.floor(y / block) * cols + Math.floor(x / block)]!;
      const offset = y * (width * 3 + 1) + 1 + x * 3;
      raw[offset] = c[0]!;
      raw[offset + 1] = c[1]!;
      raw[offset + 2] = c[2]!;
    }
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header.set([8, 2, 0, 0, 0], 8); // 8-bit RGB
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

/**
 * A project with a generated model whose version 2 is ``glb`` (a model made for the
 * test, saved through the normal versions API and validated like any other).
 */
export async function modelWithVersion(request: APIRequestContext, name: string, glb: Buffer) {
  const project = await (
    await request.post('/api/v1/projects', {
      data: { name, input_mode: 'single_image', allow_cloud_upload: true },
    })
  ).json();
  const upload = await request.post(`/api/v1/projects/${project.id}/images`, {
    multipart: {
      files: { name: 'p.png', mimeType: 'image/png', buffer: texturedPng(300, 200, 2) },
    },
  });
  const imageId = (await upload.json()).results[0].image.id as string;
  const job = await (
    await request.post(`/api/v1/projects/${project.id}/generations`, {
      data: { inputs: [{ image_id: imageId }], provider: 'mock', cloud_upload_consent: true },
    })
  ).json();
  await expect
    .poll(async () => (await (await request.get(`/api/v1/jobs/${job.id}`)).json()).status)
    .toBe('succeeded');
  const generated = (await (await request.get(`/api/v1/projects/${project.id}/models`)).json())
    .items[0];
  const saved = await request.post(`/api/v1/models/${generated.id}/versions`, {
    multipart: {
      file: { name: 'm.glb', mimeType: 'model/gltf-binary', buffer: glb },
      parent_version_id: generated.current_version.id,
    },
  });
  expect(saved.status(), await saved.text()).toBe(201);
  return { projectId: project.id as string, modelId: generated.id as string };
}
