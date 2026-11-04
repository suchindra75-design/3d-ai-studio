import { createApiClient } from './client';
import { ApiError } from './errors';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('createApiClient', () => {
  it('builds URLs with query parameters and skips empty values', async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ items: [], total: 0 }));
    const api = createApiClient({ baseUrl: 'http://127.0.0.1:8000/', fetch });
    await api.projects.list({ status: 'active', q: '', limit: 10 });
    expect(fetch).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/projects?status=active&limit=10',
      expect.objectContaining({ method: 'GET' }),
    );
    expect(api.url('/api/v1/images/x/file')).toBe('http://127.0.0.1:8000/api/v1/images/x/file');
  });

  it('sends JSON bodies', async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ id: '1' }, 201));
    const api = createApiClient({ baseUrl: '', fetch });
    await api.projects.create({ name: 'Mug', input_mode: 'single_image' });
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe('/api/v1/projects');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body)).toEqual({ name: 'Mug', input_mode: 'single_image' });
  });

  it('returns undefined for 204 responses', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    const api = createApiClient({ baseUrl: '', fetch });
    await expect(api.projects.delete('p1')).resolves.toBeUndefined();
  });

  it('turns the backend error shape into ApiError', async () => {
    const fetch = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          error: {
            code: 'not_found',
            message: 'Project not found.',
            details: { project_id: 'p1' },
            request_id: 'abc',
          },
        },
        404,
      ),
    );
    const api = createApiClient({ baseUrl: '', fetch });
    const error = await api.projects.get('p1').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 404,
      code: 'not_found',
      message: 'Project not found.',
      requestId: 'abc',
      isNetworkError: false,
    });
  });

  it('handles non-JSON error bodies', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response('<html>Bad gateway</html>', { status: 502 }));
    const api = createApiClient({ baseUrl: '', fetch });
    await expect(api.health()).rejects.toMatchObject({ status: 502, code: 'http_error' });
  });

  it('reports an unreachable backend as a network error', async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'));
    const api = createApiClient({ baseUrl: '', fetch });
    const error = (await api.health().catch((e: unknown) => e)) as ApiError;
    expect(error.isNetworkError).toBe(true);
    expect(error.code).toBe('network_error');
  });
});

describe('uploadImage', () => {
  class FakeXHR {
    static last: FakeXHR;
    upload: { onprogress: ((e: ProgressEvent) => void) | null } = { onprogress: null };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    responseType = '';
    status = 0;
    responseText = '';
    method = '';
    url = '';
    body: FormData | null = null;
    constructor() {
      FakeXHR.last = this;
    }
    open(method: string, url: string) {
      this.method = method;
      this.url = url;
    }
    send(body: FormData) {
      this.body = body;
    }
    abort() {
      this.onabort?.();
    }
  }

  beforeEach(() => vi.stubGlobal('XMLHttpRequest', FakeXHR));
  afterEach(() => vi.unstubAllGlobals());

  it('reports progress and resolves with the upload response', async () => {
    const api = createApiClient({ baseUrl: 'http://api' });
    const progress: number[] = [];
    const promise = api.images.upload('p1', new File(['x'], 'a.jpg'), {
      onProgress: (f) => progress.push(f),
    });
    const xhr = FakeXHR.last;
    expect(xhr.method).toBe('POST');
    expect(xhr.url).toBe('http://api/api/v1/projects/p1/images');
    expect((xhr.body!.get('files') as File).name).toBe('a.jpg');

    xhr.upload.onprogress!({ lengthComputable: true, loaded: 50, total: 100 } as ProgressEvent);
    xhr.status = 200;
    xhr.responseText = JSON.stringify({ results: [], created: 0, duplicates: 0, rejected: 0 });
    xhr.onload!();

    await expect(promise).resolves.toMatchObject({ created: 0 });
    expect(progress).toEqual([0.5, 1]);
  });

  it('rejects with ApiError for HTTP errors and network failures', async () => {
    const api = createApiClient({ baseUrl: '' });
    const failing = api.images.upload('p1', new File(['x'], 'a.jpg'));
    FakeXHR.last.status = 413;
    FakeXHR.last.responseText = JSON.stringify({
      error: { code: 'payload_too_large', message: 'Too big', details: {}, request_id: null },
    });
    FakeXHR.last.onload!();
    await expect(failing).rejects.toMatchObject({ status: 413, code: 'payload_too_large' });

    const offline = api.images.upload('p1', new File(['x'], 'a.jpg'));
    FakeXHR.last.onerror!();
    await expect(offline).rejects.toMatchObject({ isNetworkError: true });
  });

  it('can be aborted', async () => {
    const api = createApiClient({ baseUrl: '' });
    const controller = new AbortController();
    const upload = api.images.upload('p1', new File(['x'], 'a.jpg'), { signal: controller.signal });
    controller.abort();
    await expect(upload).rejects.toMatchObject({ name: 'AbortError' });
  });
  it('sends an Idempotency-Key when starting a generation', async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ id: 'job-1' }, 201));
    const api = createApiClient({ baseUrl: '', fetch });
    await api.generations.create(
      'p1',
      { inputs: [{ image_id: 'i1' }], cloud_upload_consent: true },
      'key-1',
    );
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe('/api/v1/projects/p1/generations');
    expect(init.headers).toEqual({
      'Idempotency-Key': 'key-1',
      'Content-Type': 'application/json',
    });
  });

  it('stores provider keys with PUT and never in the URL', async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ key: 'tripo' }));
    const api = createApiClient({ baseUrl: '', fetch });
    await api.providers.setApiKey('tripo', 'tsk_secret');
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe('/api/v1/providers/tripo/api-key');
    expect(url).not.toContain('tsk_secret');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body)).toEqual({ api_key: 'tsk_secret' });
  });

  it('saves an edited model as multipart form data without a JSON content type', async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ id: 'v2' }, 201));
    const api = createApiClient({ baseUrl: '', fetch });
    const glb = new Blob([new Uint8Array([0x67, 0x6c, 0x54, 0x46])], { type: 'model/gltf-binary' });
    await api.models.saveVersion('m1', glb, {
      parentVersionId: 'v1',
      changes: ['Moved Mug', 'Changed colour'],
    });
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe('/api/v1/models/m1/versions');
    expect(init.method).toBe('POST');
    expect(init.headers).toBeUndefined(); // the browser sets the multipart boundary
    const form = init.body as FormData;
    expect(form.get('parent_version_id')).toBe('v1');
    expect(form.getAll('changes')).toEqual(['Moved Mug', 'Changed colour']);
    expect((form.get('file') as File).name).toBe('model.glb');
  });

  it('switches versions with PATCH', async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ id: 'm1' }));
    const api = createApiClient({ baseUrl: '', fetch });
    await api.models.update('m1', { current_version_id: 'v1' });
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe('/api/v1/models/m1');
    expect(init.method).toBe('PATCH');
    expect(JSON.parse(init.body)).toEqual({ current_version_id: 'v1' });
  });

  it('builds the job stream URL', () => {
    const api = createApiClient({ baseUrl: 'http://127.0.0.1:8000', fetch: vi.fn() });
    expect(api.jobs.streamUrl('j1')).toBe('http://127.0.0.1:8000/api/v1/jobs/j1/stream');
  });
});

describe('desktop token', () => {
  const token = 'a'.repeat(64);

  it('sends the token as a header, never in request URLs', async () => {
    const fetch = vi.fn().mockResolvedValue(jsonResponse({ items: [], total: 0 }));
    const api = createApiClient({ baseUrl: 'http://127.0.0.1:54321', token, fetch });
    await api.projects.list({ q: 'mug' });
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe('http://127.0.0.1:54321/api/v1/projects?q=mug');
    expect(init.headers).toEqual({ Authorization: `Bearer ${token}` });
  });

  it('adds it to URLs the browser loads itself', () => {
    const api = createApiClient({ baseUrl: 'http://127.0.0.1:54321', token, fetch: vi.fn() });
    expect(api.url('/api/v1/images/i1/thumbnail')).toBe(
      `http://127.0.0.1:54321/api/v1/images/i1/thumbnail?access_token=${token}`,
    );
    expect(api.url('/api/v1/x?after=3')).toBe(
      `http://127.0.0.1:54321/api/v1/x?after=3&access_token=${token}`,
    );
    expect(api.jobs.streamUrl('j1')).toContain(`access_token=${token}`);
  });

  it('uploads with the token header', () => {
    const open = vi.fn();
    const setRequestHeader = vi.fn();
    vi.stubGlobal(
      'XMLHttpRequest',
      class {
        upload = {};
        open = open;
        setRequestHeader = setRequestHeader;
        send = vi.fn();
      },
    );
    const api = createApiClient({ baseUrl: 'http://127.0.0.1:1', token, fetch: vi.fn() });
    void api.images.upload('p1', new File(['x'], 'a.jpg'));
    expect(open).toHaveBeenCalledWith('POST', 'http://127.0.0.1:1/api/v1/projects/p1/images');
    expect(setRequestHeader).toHaveBeenCalledWith('Authorization', `Bearer ${token}`);
    vi.unstubAllGlobals();
  });
});
