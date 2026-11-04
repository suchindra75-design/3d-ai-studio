import type { JobRead } from '@ai3d/shared-types';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { fakeApi, renderWithStudio } from '../test/render';
import { JobList } from './job-list';

function job(overrides: Partial<JobRead> = {}): JobRead {
  return {
    id: 'j1',
    project_id: 'p1',
    kind: 'image_to_3d',
    status: 'running',
    stage: 'generating',
    progress: 0.42,
    quality_mode: 'high_quality',
    processing_mode: 'auto',
    provider: { key: 'tripo', display_name: 'Tripo AI', kind: 'cloud_api', uploads_images: true },
    provider_task_id: 'task-1',
    inputs: [{ position: 0, image_id: 'i1', image_sha256: 'x', role: 'source' }],
    provider_settings: {},
    provider_summary: [],
    estimate: { amount: 40, unit: 'credits', usd: 0.4, basis: '', notes: [] },
    cost: null,
    warnings: [],
    cloud_upload_consented: true,
    consent_reason: null,
    cancel_requested: false,
    error: null,
    attempt: 1,
    parent_job_id: null,
    result_model_version_id: null,
    source_model_version_id: null,
    result_model_id: null,
    can_cancel: true,
    can_retry: false,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    queued_at: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

it('shows live progress and cancels', async () => {
  const api = fakeApi({
    jobs: { cancel: vi.fn().mockResolvedValue(job({ status: 'cancelling' })) },
  });
  renderWithStudio(<JobList jobs={[job()]} projectId="p1" />, api);
  expect(screen.getByText('Running')).toBeInTheDocument();
  expect(screen.getByText('Generating · 42%')).toBeInTheDocument();
  expect(screen.getByText('Estimated: ≈ 40 credits (US$0.40)')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(api.jobs.cancel).toHaveBeenCalledWith('j1');
});

it('asks for permission before a waiting job uploads anything', async () => {
  const api = fakeApi({ jobs: { consent: vi.fn().mockResolvedValue(job()) } });
  renderWithStudio(
    <JobList
      jobs={[job({ status: 'waiting_for_consent', consent_reason: 'project_local_only' })]}
      projectId="p1"
    />,
    api,
  );
  expect(screen.getByText(/Nothing has been uploaded yet/)).toBeInTheDocument();
  const allow = screen.getByRole('button', { name: 'Allow and start' });
  expect(allow).toBeDisabled();
  await userEvent.click(screen.getByRole('checkbox'));
  await userEvent.click(allow);
  expect(api.jobs.consent).toHaveBeenCalledWith('j1', { allow_project_cloud_upload: true });
});

it('shows the error and confirms before retrying a cloud job', async () => {
  const api = fakeApi({ jobs: { retry: vi.fn().mockResolvedValue(job()) } });
  renderWithStudio(
    <JobList
      jobs={[
        job({
          status: 'failed',
          can_cancel: false,
          can_retry: true,
          error: {
            code: 'insufficient_credits',
            message: 'Not enough credits.',
            details: {},
            request_id: null,
          },
          cost: null,
        }),
      ]}
      projectId="p1"
    />,
    api,
  );
  expect(screen.getByRole('alert')).toHaveTextContent('Not enough credits.');
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(screen.getByText(/sends 1 photo to Tripo AI again/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Run again' }));
  await waitFor(() => expect(api.jobs.retry).toHaveBeenCalled());
  expect(vi.mocked(api.jobs.retry).mock.calls[0]!.slice(0, 2)).toEqual([
    'j1',
    { cloud_upload_consent: true },
  ]);
});
