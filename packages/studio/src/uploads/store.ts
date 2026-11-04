'use client';

import type { ApiClient } from '@ai3d/api-client';
import { ApiError } from '@ai3d/api-client';
import type { ImageRead } from '@ai3d/shared-types';
import { checkImageFile } from '@ai3d/utilities';
import { create } from 'zustand';

export type UploadStatus = 'queued' | 'uploading' | 'done' | 'duplicate' | 'error';

export interface UploadItem {
  id: string;
  projectId: string;
  name: string;
  size: number;
  status: UploadStatus;
  progress: number;
  error?: string;
  image?: ImageRead;
}

interface UploadState {
  items: UploadItem[];
  patch: (id: string, changes: Partial<UploadItem>) => void;
  add: (items: UploadItem[]) => void;
  clearFinished: (projectId: string) => void;
}

export const useUploadStore = create<UploadState>((set) => ({
  items: [],
  patch: (id, changes) =>
    set((state) => ({
      items: state.items.map((item) => (item.id === id ? { ...item, ...changes } : item)),
    })),
  add: (items) => set((state) => ({ items: [...state.items, ...items] })),
  clearFinished: (projectId) =>
    set((state) => ({
      items: state.items.filter(
        (item) =>
          item.projectId !== projectId || item.status === 'queued' || item.status === 'uploading',
      ),
    })),
}));

const CONCURRENCY = 2;
let counter = 0;

/**
 * Validates files locally, queues them and uploads with limited concurrency.
 * Calls `onSettled` after each file so callers can refresh queries.
 */
export async function enqueueUploads(options: {
  api: ApiClient;
  projectId: string;
  files: File[];
  maxBytes: number;
  onSettled: () => void;
}): Promise<void> {
  const { api, projectId, files, maxBytes, onSettled } = options;
  const store = useUploadStore.getState();
  const jobs: { item: UploadItem; file: File }[] = [];

  for (const file of files) {
    const check = checkImageFile(file, maxBytes);
    counter += 1;
    const item: UploadItem = {
      id: `upload-${Date.now()}-${counter}`,
      projectId,
      name: file.name,
      size: file.size,
      status: check.ok ? 'queued' : 'error',
      progress: 0,
      error: check.reason,
    };
    store.add([item]);
    if (check.ok) jobs.push({ item, file });
  }

  async function worker() {
    for (let job = jobs.shift(); job; job = jobs.shift()) {
      const { item, file } = job;
      const { patch } = useUploadStore.getState();
      patch(item.id, { status: 'uploading' });
      try {
        const response = await api.images.upload(projectId, file, {
          onProgress: (fraction) => patch(item.id, { progress: fraction }),
        });
        const result = response.results[0];
        if (!result || result.status === 'rejected') {
          patch(item.id, {
            status: 'error',
            error: result?.error?.message ?? 'The server rejected this file.',
          });
        } else {
          patch(item.id, {
            status: result.status === 'duplicate' ? 'duplicate' : 'done',
            progress: 1,
            image: result.image ?? undefined,
          });
        }
      } catch (error) {
        patch(item.id, {
          status: 'error',
          error: error instanceof ApiError ? error.message : 'Upload failed.',
        });
      }
      onSettled();
    }
  }

  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, jobs.length) }, worker));
}
