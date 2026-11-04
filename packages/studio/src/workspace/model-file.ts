import type { ApiClient } from '@ai3d/api-client';
import { disposeObject, parseGlb, type LoadedModel } from '@ai3d/viewer';
import { useEffect, useState } from 'react';

export type ModelFileState =
  | { status: 'loading'; progress: number | null }
  | { status: 'error'; message: string }
  | { status: 'ready'; model: LoadedModel };

async function download(
  url: string,
  signal: AbortSignal,
  onProgress: (fraction: number | null) => void,
): Promise<ArrayBuffer> {
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(`The backend returned ${response.status} for the model file.`);
  // Progress is only shown when the size is known (a proxy may drop the header).
  const total = Number(response.headers.get('Content-Length')) || 0;
  if (!response.body || !total) return response.arrayBuffer();
  const reader = response.body.getReader();
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    onProgress(Math.min(received / total, 1));
  }
  return new Blob(chunks).arrayBuffer();
}

/** Downloads and parses one version's GLB; the previous model is freed on change. */
export function useModelFile(api: ApiClient, fileUrl: string | null): ModelFileState {
  const [result, setResult] = useState<{ url: string; state: ModelFileState } | null>(null);

  useEffect(() => {
    if (!fileUrl) return undefined;
    const controller = new AbortController();
    let loaded: LoadedModel | null = null;
    const report = (state: ModelFileState) => {
      if (!controller.signal.aborted) setResult({ url: fileUrl, state });
    };
    download(api.url(fileUrl), controller.signal, (progress) =>
      report({ status: 'loading', progress }),
    )
      .then((data) => parseGlb(data))
      .then((model) => {
        if (controller.signal.aborted) {
          disposeObject(model.root);
          return;
        }
        loaded = model;
        report({ status: 'ready', model });
      })
      .catch((error: unknown) =>
        report({
          status: 'error',
          message: error instanceof Error ? error.message : 'The model could not be loaded.',
        }),
      );
    return () => {
      controller.abort();
      if (loaded) disposeObject(loaded.root);
    };
  }, [api, fileUrl]);

  if (!fileUrl) return { status: 'error', message: 'This version has no 3D file.' };
  if (result?.url !== fileUrl) return { status: 'loading', progress: null };
  return result.state;
}
