'use client';

import type { ListProjectsParams } from '@ai3d/api-client';
import type {
  EnhanceRequest,
  ExportFormat,
  GenerationCreate,
  JobRead,
  ModelUpdate,
  JobStatus,
  ProjectCreate,
  ProjectUpdate,
} from '@ai3d/shared-types';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef } from 'react';

import { useApi } from '../context';

export const queryKeys = {
  health: ['health'] as const,
  systemInfo: ['system-info'] as const,
  localDevice: ['devices', 'local'] as const,
  projects: ['projects'] as const,
  projectList: (params: ListProjectsParams) => ['projects', 'list', params] as const,
  project: (id: string) => ['projects', 'detail', id] as const,
  images: (projectId: string) => ['projects', 'detail', projectId, 'images'] as const,
  capture: (projectId: string) => ['projects', 'detail', projectId, 'capture'] as const,
  jobs: (projectId: string) => ['projects', 'detail', projectId, 'jobs'] as const,
  models: (projectId: string) => ['projects', 'detail', projectId, 'models'] as const,
  plan: (projectId: string, body: GenerationCreate) =>
    ['projects', 'detail', projectId, 'plan', body] as const,
  jobEvents: (jobId: string) => ['jobs', jobId, 'events'] as const,
  model: (id: string) => ['models', id] as const,
  job: (id: string) => ['jobs', id] as const,
  exportFormats: ['export-formats'] as const,
  exports: (versionId: string) => ['model-versions', versionId, 'exports'] as const,
  modelVersions: (id: string) => ['models', id, 'versions'] as const,
  providers: ['providers'] as const,
};

const ACTIVE_STATUSES: ReadonlySet<JobStatus> = new Set(['queued', 'running', 'cancelling']);

export function isJobActive(job: Pick<JobRead, 'status'>): boolean {
  return ACTIVE_STATUSES.has(job.status);
}

export function useHealth() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: ({ signal }) => api.health(signal),
    refetchInterval: 15_000,
    retry: false,
  });
}

export function useSystemInfo() {
  const api = useApi();
  return useQuery({ queryKey: queryKeys.systemInfo, queryFn: () => api.systemInfo() });
}

export function useLocalDevice() {
  const api = useApi();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.localDevice,
    queryFn: () => api.devices.local(),
    staleTime: 60_000,
  });
  const refresh = useMutation({
    mutationFn: () => api.devices.local(true),
    onSuccess: (device) => client.setQueryData(queryKeys.localDevice, device),
  });
  return { ...query, refresh };
}

export function useProjects(params: ListProjectsParams) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.projectList(params),
    queryFn: () => api.projects.list(params),
    placeholderData: (previous) => previous,
  });
}

export function useProject(id: string) {
  const api = useApi();
  return useQuery({ queryKey: queryKeys.project(id), queryFn: () => api.projects.get(id) });
}

export function useCreateProject() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ProjectCreate) => api.projects.create(body),
    onSuccess: (project) => {
      client.setQueryData(queryKeys.project(project.id), project);
      void client.invalidateQueries({ queryKey: queryKeys.projects });
    },
  });
}

export function useUpdateProject(id: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ProjectUpdate) => api.projects.update(id, body),
    onSuccess: (project) => {
      client.setQueryData(queryKeys.project(id), project);
      void client.invalidateQueries({ queryKey: queryKeys.projects });
    },
  });
}

export function useDeleteProject() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.projects.delete(id),
    onSuccess: (_data, id) => {
      client.removeQueries({ queryKey: queryKeys.project(id) });
      void client.invalidateQueries({ queryKey: queryKeys.projects });
    },
  });
}

export function useImages(projectId: string) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.images(projectId),
    queryFn: () => api.images.list(projectId),
  });
}

export function useCaptureAssessment(projectId: string) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.capture(projectId),
    queryFn: () => api.projects.captureAssessment(projectId),
  });
}

export function useMeasureOverlap(projectId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.projects.measureOverlap(projectId),
    onSuccess: (assessment) => client.setQueryData(queryKeys.capture(projectId), assessment),
  });
}

export function useDeleteImage() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (imageId: string) => api.images.delete(imageId),
    onSuccess: () => invalidateProjectData(client),
  });
}

/** Refreshes project cards, details (counts, cover), images and capture assessments. */
export function invalidateProjectData(client: ReturnType<typeof useQueryClient>) {
  return client.invalidateQueries({ queryKey: queryKeys.projects });
}

// -- providers ------------------------------------------------------------------

export function useProviders() {
  const api = useApi();
  return useQuery({ queryKey: queryKeys.providers, queryFn: () => api.providers.list() });
}

export function useSetProviderKey() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ key, apiKey }: { key: string; apiKey: string }) =>
      api.providers.setApiKey(key, apiKey),
    onSuccess: () => void client.invalidateQueries({ queryKey: queryKeys.providers }),
  });
}

export function useDeleteProviderKey() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (key: string) => api.providers.deleteApiKey(key),
    onSuccess: () => void client.invalidateQueries({ queryKey: queryKeys.providers }),
  });
}

export function useCheckProvider() {
  const api = useApi();
  return useMutation({ mutationFn: (key: string) => api.providers.check(key) });
}

// -- generation & jobs --------------------------------------------------------------

export function useGenerationPlan(projectId: string, body: GenerationCreate | null) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.plan(projectId, body ?? { inputs: [] }),
    queryFn: () => api.generations.plan(projectId, body!),
    enabled: body !== null && body.inputs.length > 0,
    placeholderData: (previous) => previous,
  });
}

export function useCreateGeneration(projectId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ body, idempotencyKey }: { body: GenerationCreate; idempotencyKey: string }) =>
      api.generations.create(projectId, body, idempotencyKey),
    onSuccess: () => void client.invalidateQueries({ queryKey: queryKeys.jobs(projectId) }),
  });
}

export function useJobs(projectId: string) {
  const api = useApi();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.jobs(projectId),
    queryFn: () => api.jobs.list(projectId),
    // Fallback when live events are unavailable; the stream refreshes sooner.
    refetchInterval: (query) =>
      query.state.data?.items.some((job) => isJobActive(job)) ? 4000 : false,
  });
  // A job that newly succeeded has produced a model: refresh models and project
  // counts, however the status change arrived (event stream or polling).
  const succeeded = query.data?.items
    .filter((job) => job.status === 'succeeded')
    .map((job) => job.id)
    .join(',');
  const previous = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (succeeded === undefined) return;
    if (previous.current !== undefined && previous.current !== succeeded) {
      void client.invalidateQueries({ queryKey: queryKeys.models(projectId) });
      void client.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    }
    previous.current = succeeded;
  }, [client, projectId, succeeded]);
  return query;
}

export function useJobEvents(jobId: string, enabled: boolean) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.jobEvents(jobId),
    queryFn: () => api.jobs.events(jobId),
    enabled,
  });
}

/**
 * Subscribes to a running job's Server-Sent Events and refreshes the project's
 * jobs (and models, when the job ends). Without EventSource it does nothing;
 * ``useJobs`` keeps polling instead.
 */
export function useJobStream(projectId: string, jobId: string, active: boolean) {
  const api = useApi();
  const client = useQueryClient();
  useEffect(() => {
    if (!active || typeof EventSource === 'undefined') return undefined;
    const source = new EventSource(api.jobs.streamUrl(jobId));
    const refresh = () => {
      void client.invalidateQueries({ queryKey: queryKeys.jobs(projectId) });
      void client.invalidateQueries({ queryKey: queryKeys.jobEvents(jobId) });
    };
    for (const type of ['status', 'stage', 'progress', 'log', 'warning', 'error']) {
      source.addEventListener(type, refresh);
    }
    source.addEventListener('end', () => {
      source.close();
      refresh();
      void client.invalidateQueries({ queryKey: queryKeys.models(projectId) });
      void client.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    });
    return () => source.close();
  }, [api, client, projectId, jobId, active]);
}

function useJobMutation<TArgs>(projectId: string, fn: (args: TArgs) => Promise<JobRead>) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.jobs(projectId) });
      void client.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });
}

export function useCancelJob(projectId: string) {
  const api = useApi();
  return useJobMutation(projectId, (jobId: string) => api.jobs.cancel(jobId));
}

export function useConsentJob(projectId: string) {
  const api = useApi();
  return useJobMutation(
    projectId,
    ({ jobId, allowProject }: { jobId: string; allowProject: boolean }) =>
      api.jobs.consent(jobId, { allow_project_cloud_upload: allowProject }),
  );
}

export function useRetryJob(projectId: string) {
  const api = useApi();
  return useJobMutation(
    projectId,
    ({
      jobId,
      consent,
      idempotencyKey,
      provider,
    }: {
      jobId: string;
      consent: boolean;
      idempotencyKey: string;
      /** Another engine the failed job offered (``JobRead.alternatives``). */
      provider?: string;
    }) =>
      api.jobs.retry(
        jobId,
        provider ? { cloud_upload_consent: consent, provider } : { cloud_upload_consent: consent },
        idempotencyKey,
      ),
  );
}

// -- models ---------------------------------------------------------------------

export function useModels(projectId: string) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.models(projectId),
    queryFn: () => api.models.list(projectId),
  });
}

export function useDeleteModel(projectId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (modelId: string) => api.models.delete(modelId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.models(projectId) });
      void client.invalidateQueries({ queryKey: queryKeys.jobs(projectId) });
    },
  });
}

export function useModel(modelId: string) {
  const api = useApi();
  return useQuery({ queryKey: queryKeys.model(modelId), queryFn: () => api.models.get(modelId) });
}

export function useModelVersions(modelId: string) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.modelVersions(modelId),
    queryFn: () => api.models.versions(modelId),
  });
}

function useInvalidateModel(projectId: string, modelId: string) {
  const client = useQueryClient();
  return () => {
    // Prefix match: the model and its version list.
    void client.invalidateQueries({ queryKey: queryKeys.model(modelId) });
    void client.invalidateQueries({ queryKey: queryKeys.models(projectId) });
    void client.invalidateQueries({ queryKey: queryKeys.project(projectId) });
  };
}

export function useUpdateModel(projectId: string, modelId: string) {
  const api = useApi();
  const invalidate = useInvalidateModel(projectId, modelId);
  return useMutation({
    mutationFn: (body: ModelUpdate) => api.models.update(modelId, body),
    onSuccess: invalidate,
  });
}

export function useImportModel(projectId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      ...options
    }: {
      file: File;
      units?: 'm' | 'cm' | 'mm' | 'in';
      upAxis?: 'y' | 'z';
      realScale?: boolean;
    }) => api.models.importFile(projectId, file, file.name, options),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.models(projectId) });
      void client.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });
}

export function useRestoreProject() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => api.projects.restore(file, file.name),
    onSuccess: () => void client.invalidateQueries({ queryKey: queryKeys.projects }),
  });
}

export function useSaveModelVersion(projectId: string, modelId: string) {
  const api = useApi();
  const invalidate = useInvalidateModel(projectId, modelId);
  return useMutation({
    mutationFn: ({
      file,
      parentVersionId,
      changes,
    }: {
      file: Blob;
      parentVersionId: string;
      changes: string[];
    }) => api.models.saveVersion(modelId, file, { parentVersionId, changes }),
    onSuccess: invalidate,
  });
}

// -- enhance & export -----------------------------------------------------------

export function useEnhancePlan(modelId: string, body: EnhanceRequest) {
  const api = useApi();
  return useQuery({
    queryKey: ['models', modelId, 'enhance-plan', body] as const,
    queryFn: () => api.models.enhancePlan(modelId, body),
    placeholderData: (previous) => previous,
  });
}

export function useEnhance(projectId: string, modelId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ body, idempotencyKey }: { body: EnhanceRequest; idempotencyKey: string }) =>
      api.models.enhance(modelId, body, idempotencyKey),
    onSuccess: (job) => {
      client.setQueryData(queryKeys.job(job.id), job);
      void client.invalidateQueries({ queryKey: queryKeys.jobs(projectId) });
    },
  });
}

/** One job, polled while it runs; refreshes the model when it finishes. */
export function useJob(projectId: string, modelId: string, jobId: string | null) {
  const api = useApi();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: queryKeys.job(jobId ?? ''),
    queryFn: () => api.jobs.get(jobId!),
    enabled: jobId !== null,
    refetchInterval: (q) => (q.state.data && isJobActive(q.state.data) ? 1000 : false),
  });
  const finished = query.data && !isJobActive(query.data) ? query.data.status : null;
  useEffect(() => {
    if (!finished) return;
    void client.invalidateQueries({ queryKey: queryKeys.model(modelId) });
    void client.invalidateQueries({ queryKey: queryKeys.models(projectId) });
    void client.invalidateQueries({ queryKey: queryKeys.jobs(projectId) });
  }, [client, finished, modelId, projectId]);
  return query;
}

export function useExportFormats() {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.exportFormats,
    queryFn: () => api.exports.formats(),
    staleTime: 60_000,
  });
}

export function useExports(versionId: string) {
  const api = useApi();
  return useQuery({
    queryKey: queryKeys.exports(versionId),
    queryFn: () => api.exports.list(versionId),
  });
}

export function useCreateExport(versionId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (format: ExportFormat) => api.exports.create(versionId, format),
    onSettled: () => void client.invalidateQueries({ queryKey: queryKeys.exports(versionId) }),
  });
}

export function useDeleteExport(versionId: string) {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (exportId: string) => api.exports.delete(exportId),
    onSettled: () => void client.invalidateQueries({ queryKey: queryKeys.exports(versionId) }),
  });
}
