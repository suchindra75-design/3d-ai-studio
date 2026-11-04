import { ApiError } from '@ai3d/api-client';
import type { UploadResponse } from '@ai3d/shared-types';

import { fakeApi } from '../test/render';
import { enqueueUploads, useUploadStore } from './store';

const jpeg = (name: string, size = 100) =>
  new File([new Uint8Array(size)], name, { type: 'image/jpeg' });

function result(status: 'created' | 'duplicate' | 'rejected', message?: string): UploadResponse {
  // Only the fields the store reads are filled in.
  const response = {
    results: [
      {
        filename: 'x',
        status,
        image: status === 'rejected' ? null : { id: 'img' },
        error: message ? { code: 'corrupt_image', message, details: {}, request_id: null } : null,
      },
    ],
    created: status === 'created' ? 1 : 0,
    duplicates: status === 'duplicate' ? 1 : 0,
    rejected: status === 'rejected' ? 1 : 0,
  };
  return response as unknown as UploadResponse;
}

beforeEach(() => useUploadStore.setState({ items: [] }));

describe('enqueueUploads', () => {
  it('validates locally, uploads the rest and records each outcome', async () => {
    const upload = vi
      .fn()
      .mockResolvedValueOnce(result('created'))
      .mockResolvedValueOnce(result('duplicate'))
      .mockResolvedValueOnce(result('rejected', 'Image data is corrupt or truncated.'))
      .mockRejectedValueOnce(
        new ApiError(0, { code: 'network_error', message: 'Backend offline' }),
      );
    const api = fakeApi({ images: { upload } });
    const onSettled = vi.fn();

    await enqueueUploads({
      api,
      projectId: 'p1',
      maxBytes: 1000,
      onSettled,
      files: [
        jpeg('a.jpg'),
        jpeg('b.jpg'),
        jpeg('c.jpg'),
        jpeg('d.jpg'),
        jpeg('huge.jpg', 5000),
        new File(['x'], 'notes.txt', { type: 'text/plain' }),
      ],
    });

    // Invalid files never reach the network.
    expect(upload).toHaveBeenCalledTimes(4);
    expect(onSettled).toHaveBeenCalledTimes(4);

    const byName = Object.fromEntries(useUploadStore.getState().items.map((i) => [i.name, i]));
    expect(byName['huge.jpg']).toMatchObject({
      status: 'error',
      error: expect.stringMatching(/limit/),
    });
    expect(byName['notes.txt']).toMatchObject({ status: 'error' });
    const statuses = ['a.jpg', 'b.jpg', 'c.jpg', 'd.jpg'].map((n) => byName[n]!.status).sort();
    expect(statuses).toEqual(['done', 'duplicate', 'error', 'error']);
    const errors = ['c.jpg', 'd.jpg']
      .map((n) => byName[n]!.error)
      .concat(['a.jpg', 'b.jpg'].map((n) => byName[n]!.error));
    expect(errors.filter(Boolean).sort()).toEqual(
      ['Backend offline', 'Image data is corrupt or truncated.'].sort(),
    );
  });

  it('uploads at most two files at a time', async () => {
    let active = 0;
    let peak = 0;
    const upload = vi.fn(async () => {
      active += 1;
      peak = Math.max(peak, active);
      await new Promise((r) => setTimeout(r, 5));
      active -= 1;
      return result('created');
    });
    await enqueueUploads({
      api: fakeApi({ images: { upload } }),
      projectId: 'p1',
      maxBytes: 1000,
      onSettled: () => {},
      files: Array.from({ length: 5 }, (_, i) => jpeg(`${i}.jpg`)),
    });
    expect(upload).toHaveBeenCalledTimes(5);
    expect(peak).toBe(2);
  });

  it('clearFinished keeps active uploads and other projects', () => {
    useUploadStore.setState({
      items: [
        { id: '1', projectId: 'p1', name: 'a', size: 1, status: 'done', progress: 1 },
        { id: '2', projectId: 'p1', name: 'b', size: 1, status: 'uploading', progress: 0.3 },
        { id: '3', projectId: 'p2', name: 'c', size: 1, status: 'done', progress: 1 },
      ],
    });
    useUploadStore.getState().clearFinished('p1');
    expect(useUploadStore.getState().items.map((i) => i.id)).toEqual(['2', '3']);
  });
});
