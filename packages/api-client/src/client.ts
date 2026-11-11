import type {
  CaptureAssessment,
  ConsentGrant,
  CredentialCheckRead,
  EnhancePlanRead,
  EnhanceRequest,
  ExportFormat,
  ExportFormatList,
  ExportList,
  ExportRead,
  InspectionRead,
  InspectionRequest,
  LodRead,
  GenerationCreate,
  GenerationPlanRead,
  HealthResponse,
  ImageList,
  ImageRead,
  JobEventList,
  JobList,
  JobRead,
  LocalDeviceRead,
  ModelList,
  ModelRead,
  ModelUpdate,
  ModelVersionList,
  ModelVersionRead,
  RepairRequest,
  ProjectCreate,
  ProjectList,
  ProjectRead,
  ProjectStatus,
  ProjectUpdate,
  ProviderList,
  ProviderRead,
  RetextureRequest,
  RetryRequest,
  SystemInfo,
  UploadResponse,
} from '@ai3d/shared-types';

import { networkError, parseErrorBody } from './errors';

export interface ApiClientOptions {
  /** Origin of the backend, e.g. "http://127.0.0.1:8000". Empty string = same origin. */
  baseUrl: string;
  /**
   * Per-launch secret of the desktop app's private backend. Sent as a Bearer header,
   * and added to URLs the browser loads itself (images, downloads, event streams).
   */
  token?: string;
  fetch?: typeof fetch;
}

export interface ListProjectsParams {
  status?: ProjectStatus;
  q?: string;
  limit?: number;
  offset?: number;
}

export interface UploadOptions {
  onProgress?: (fraction: number) => void;
  signal?: AbortSignal;
}

type Query = Record<string, string | number | boolean | undefined>;

export type ApiClient = ReturnType<typeof createApiClient>;

export function createApiClient({ baseUrl, token, fetch: fetchImpl }: ApiClientOptions) {
  const origin = baseUrl.replace(/\/+$/, '');
  const doFetch = fetchImpl ?? ((...args: Parameters<typeof fetch>) => fetch(...args));

  /** URL for requests that send the token as a header (it stays out of the URL). */
  function endpoint(path: string, query?: Query): string {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(query ?? {})) {
      if (value !== undefined && value !== '') search.set(key, String(value));
    }
    const qs = search.toString();
    return `${origin}${path}${qs ? `?${qs}` : ''}`;
  }

  /** URL the browser loads by itself (img src, downloads, EventSource): carries the token. */
  function url(path: string, query?: Query): string {
    const base = endpoint(path, query);
    if (!token) return base;
    return `${base}${base.includes('?') ? '&' : '?'}access_token=${encodeURIComponent(token)}`;
  }

  async function request<T>(
    method: string,
    path: string,
    options: {
      query?: Query;
      body?: unknown;
      form?: FormData;
      signal?: AbortSignal;
      headers?: Record<string, string>;
    } = {},
  ): Promise<T> {
    const headers: Record<string, string> = { ...options.headers };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    let response: Response;
    try {
      response = await doFetch(endpoint(path, options.query), {
        method,
        signal: options.signal,
        headers: Object.keys(headers).length > 0 ? headers : undefined,
        body:
          options.form ?? (options.body !== undefined ? JSON.stringify(options.body) : undefined),
      });
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === 'AbortError') throw cause;
      throw networkError(cause);
    }
    if (!response.ok) throw parseErrorBody(response.status, await response.text());
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  function uploadImage(projectId: string, file: File, options: UploadOptions = {}) {
    return new Promise<UploadResponse>((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', endpoint(`/api/v1/projects/${projectId}/images`));
      if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);
      xhr.responseType = 'text';
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) options.onProgress?.(event.loaded / event.total);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          options.onProgress?.(1);
          resolve(JSON.parse(xhr.responseText) as UploadResponse);
        } else {
          reject(parseErrorBody(xhr.status, xhr.responseText));
        }
      };
      xhr.onerror = () => reject(networkError(new Error('XMLHttpRequest failed')));
      xhr.onabort = () => reject(new DOMException('Upload aborted', 'AbortError'));
      options.signal?.addEventListener('abort', () => xhr.abort(), { once: true });

      const form = new FormData();
      form.append('files', file, file.name);
      xhr.send(form);
    });
  }

  return {
    baseUrl: origin,
    /** Absolute URL for a backend path (for <img src>, downloads). */
    url,
    health: (signal?: AbortSignal) => request<HealthResponse>('GET', '/api/v1/health', { signal }),
    systemInfo: () => request<SystemInfo>('GET', '/api/v1/system/info'),
    projects: {
      list: (params: ListProjectsParams = {}) =>
        request<ProjectList>('GET', '/api/v1/projects', { query: { ...params } }),
      get: (id: string) => request<ProjectRead>('GET', `/api/v1/projects/${id}`),
      create: (body: ProjectCreate) => request<ProjectRead>('POST', '/api/v1/projects', { body }),
      update: (id: string, body: ProjectUpdate) =>
        request<ProjectRead>('PATCH', `/api/v1/projects/${id}`, { body }),
      delete: (id: string) => request<void>('DELETE', `/api/v1/projects/${id}`),
      captureAssessment: (id: string) =>
        request<CaptureAssessment>('GET', `/api/v1/projects/${id}/capture-assessment`),
      /** Feature-matches the photos on the backend (seconds per 10 photos) and caches the result. */
      measureOverlap: (id: string) =>
        request<CaptureAssessment>('POST', `/api/v1/projects/${id}/capture-assessment/overlap`),
      /** Runs local generation's background removal on the photos (seconds per photo). */
      checkCutout: (id: string) =>
        request<CaptureAssessment>('POST', `/api/v1/projects/${id}/capture-assessment/cutout`),
      /** Backend path of the project's backup ZIP (photos, models, versions, reports). */
      backupPath: (id: string) => `/api/v1/projects/${id}/backup`,
      /** Creates a new project from a backup ZIP made by this app. */
      restore: (file: Blob, fileName: string) => {
        const form = new FormData();
        form.append('file', file, fileName);
        return request<ProjectRead>('POST', '/api/v1/projects/restore', { form });
      },
    },
    images: {
      list: (projectId: string) =>
        request<ImageList>('GET', `/api/v1/projects/${projectId}/images`),
      get: (id: string) => request<ImageRead>('GET', `/api/v1/images/${id}`),
      delete: (id: string) => request<void>('DELETE', `/api/v1/images/${id}`),
      upload: uploadImage,
    },
    devices: {
      local: (refresh = false) =>
        request<LocalDeviceRead>('GET', '/api/v1/devices/local', { query: { refresh } }),
    },
    providers: {
      list: () => request<ProviderList>('GET', '/api/v1/providers'),
      /** The key is sent once and stored encrypted by the backend; it is never returned. */
      setApiKey: (key: string, apiKey: string) =>
        request<ProviderRead>('PUT', `/api/v1/providers/${key}/api-key`, {
          body: { api_key: apiKey },
        }),
      deleteApiKey: (key: string) =>
        request<ProviderRead>('DELETE', `/api/v1/providers/${key}/api-key`),
      /** The address of a server you run (the GPU server); not secret. */
      setBaseUrl: (key: string, baseUrl: string) =>
        request<ProviderRead>('PUT', `/api/v1/providers/${key}/base-url`, {
          body: { base_url: baseUrl },
        }),
      deleteBaseUrl: (key: string) =>
        request<ProviderRead>('DELETE', `/api/v1/providers/${key}/base-url`),
      check: (key: string) =>
        request<CredentialCheckRead>('POST', `/api/v1/providers/${key}/check`),
    },
    generations: {
      plan: (projectId: string, body: GenerationCreate) =>
        request<GenerationPlanRead>('POST', `/api/v1/projects/${projectId}/generations/plan`, {
          body,
        }),
      create: (projectId: string, body: GenerationCreate, idempotencyKey?: string) =>
        request<JobRead>('POST', `/api/v1/projects/${projectId}/generations`, {
          body,
          headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
        }),
    },
    jobs: {
      list: (projectId: string) => request<JobList>('GET', `/api/v1/projects/${projectId}/jobs`),
      get: (id: string) => request<JobRead>('GET', `/api/v1/jobs/${id}`),
      events: (id: string, after = 0) =>
        request<JobEventList>('GET', `/api/v1/jobs/${id}/events`, { query: { after } }),
      /** Server-Sent Events URL for live progress. */
      streamUrl: (id: string) => url(`/api/v1/jobs/${id}/stream`),
      cancel: (id: string) => request<JobRead>('POST', `/api/v1/jobs/${id}/cancel`),
      consent: (id: string, body: ConsentGrant) =>
        request<JobRead>('POST', `/api/v1/jobs/${id}/consent`, { body }),
      retry: (id: string, body: RetryRequest, idempotencyKey?: string) =>
        request<JobRead>('POST', `/api/v1/jobs/${id}/retry`, {
          body,
          headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
        }),
    },
    models: {
      list: (projectId: string) =>
        request<ModelList>('GET', `/api/v1/projects/${projectId}/models`),
      get: (id: string) => request<ModelRead>('GET', `/api/v1/models/${id}`),
      update: (id: string, body: ModelUpdate) =>
        request<ModelRead>('PATCH', `/api/v1/models/${id}`, { body }),
      delete: (id: string) => request<void>('DELETE', `/api/v1/models/${id}`),
      versions: (id: string) => request<ModelVersionList>('GET', `/api/v1/models/${id}/versions`),
      /** Saves an edited GLB as a new version; the parent version is kept. */
      /** Imports an existing 3D file (GLB, OBJ, STL, PLY, 3MF or ZIP) as a new model. */
      importFile: (
        projectId: string,
        file: Blob,
        fileName: string,
        options: {
          units?: 'm' | 'cm' | 'mm' | 'in';
          upAxis?: 'y' | 'z';
          realScale?: boolean;
          name?: string;
        } = {},
      ) => {
        const form = new FormData();
        form.append('file', file, fileName);
        if (options.units) form.append('units', options.units);
        if (options.upAxis) form.append('up_axis', options.upAxis);
        if (options.realScale) form.append('real_scale', 'true');
        if (options.name) form.append('name', options.name);
        return request<ModelRead>('POST', `/api/v1/projects/${projectId}/models/import`, { form });
      },
      saveVersion: (
        id: string,
        file: Blob,
        { parentVersionId, changes }: { parentVersionId: string; changes: string[] },
      ) => {
        const form = new FormData();
        form.append('file', file, 'model.glb');
        form.append('parent_version_id', parentVersionId);
        for (const change of changes) form.append('changes', change);
        return request<ModelVersionRead>('POST', `/api/v1/models/${id}/versions`, { form });
      },
      /** A cloud service re-textures a version (uploads the model; asks first). */
      retexture: (id: string, body: RetextureRequest) =>
        request<JobRead>('POST', `/api/v1/models/${id}/retexture`, { body }),
      enhancePlan: (id: string, body: EnhanceRequest) =>
        request<EnhancePlanRead>('POST', `/api/v1/models/${id}/enhance/plan`, { body }),
      /** Starts a background job; the source version is never changed. */
      enhance: (id: string, body: EnhanceRequest, idempotencyKey?: string) =>
        request<JobRead>('POST', `/api/v1/models/${id}/enhance`, {
          body,
          headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
        }),
    },
    inspection: {
      /** The version's health report (status "missing" until inspected, "running" meanwhile). */
      get: (versionId: string) =>
        request<InspectionRead>('GET', `/api/v1/model-versions/${versionId}/inspection`),
      /** Starts inspecting in the background; nothing is changed. */
      start: (versionId: string, body: InspectionRequest) =>
        request<InspectionRead>('POST', `/api/v1/model-versions/${versionId}/inspection`, {
          body,
        }),
    },
    repairs: {
      /** A repair job: only the operations given; the result is a new version. */
      create: (modelId: string, body: RepairRequest, idempotencyKey?: string) =>
        request<JobRead>('POST', `/api/v1/models/${modelId}/repairs`, {
          body,
          headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : undefined,
        }),
    },
    lod: {
      /** Levels of detail for viewing a large version (status "missing" until built). */
      get: (versionId: string) =>
        request<LodRead>('GET', `/api/v1/model-versions/${versionId}/lod`),
      /** Builds them once; can take seconds for models of millions of triangles. */
      build: (versionId: string) =>
        request<LodRead>('POST', `/api/v1/model-versions/${versionId}/lod`),
    },
    exports: {
      formats: () => request<ExportFormatList>('GET', '/api/v1/exports/formats'),
      list: (versionId: string) =>
        request<ExportList>('GET', `/api/v1/model-versions/${versionId}/exports`),
      /** Converts, re-opens and compares the file; a mismatch comes back as status "failed". */
      create: (versionId: string, format: ExportFormat) =>
        request<ExportRead>('POST', `/api/v1/model-versions/${versionId}/exports`, {
          body: { format },
        }),
      delete: (id: string) => request<void>('DELETE', `/api/v1/exports/${id}`),
    },
  };
}
