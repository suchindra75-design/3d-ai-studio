import type { GenerationPlanRead, ImageRead, ProjectRead, ProviderList } from '@ai3d/shared-types';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { fakeApi, renderWithStudio } from '../test/render';
import { GenerationPanel } from './generation-panel';

const project = {
  id: 'p1',
  name: 'Mug',
  input_mode: 'single_image',
  default_quality_mode: 'fast',
  default_processing_mode: 'auto',
  allow_cloud_upload: false,
} as ProjectRead;

const images = [
  { id: 'i1', original_filename: 'mug.jpg' },
  { id: 'i2', original_filename: 'mug-2.jpg' },
] as ImageRead[];

const providers = {
  items: [
    { key: 'tripo', display_name: 'Tripo AI', configured: true },
    { key: 'meshy', display_name: 'Meshy AI', configured: false },
  ],
} as ProviderList;

function plan(overrides: Partial<GenerationPlanRead> = {}): GenerationPlanRead {
  return {
    ok: true,
    kind: 'image_to_3d',
    quality_mode: 'fast',
    processing_mode: 'auto',
    route: 'cloud',
    provider: { key: 'tripo', display_name: 'Tripo AI', kind: 'cloud_api', uploads_images: true },
    inputs: [{ image_id: 'i1', role: 'source', filename: 'mug.jpg', quality_level: 'good' }],
    provider_settings: {},
    provider_summary: ['Model v3.1-20260211', 'Standard texture'],
    estimate: { amount: 30, unit: 'credits', usd: 0.3, basis: 'Published price list', notes: [] },
    requires_consent: true,
    consent_reason: 'project_local_only',
    provenance: 'inferred',
    warnings: ['From one photo, the sides the camera did not see are invented by the AI.'],
    blocking_issues: [],
    ...overrides,
  };
}

it('requires both consents before sending photos from a local-only project', async () => {
  const api = fakeApi({
    providers: { list: vi.fn().mockResolvedValue(providers) },
    generations: {
      plan: vi.fn().mockResolvedValue(plan()),
      create: vi.fn().mockResolvedValue({ id: 'job-1' }),
    },
    projects: { update: vi.fn().mockResolvedValue({ ...project, allow_cloud_upload: true }) },
  });
  renderWithStudio(<GenerationPanel project={project} images={images} />, api);

  expect(await screen.findByText('≈ 30 credits (US$0.30)', { exact: false })).toBeInTheDocument();
  expect(screen.getByText(/sides the camera did not see are invented/)).toBeInTheDocument();
  expect(screen.getByRole('option', { name: 'Meshy AI (no API key)' })).toBeDisabled();

  const generate = screen.getByRole('button', { name: 'Generate' });
  expect(generate).toBeDisabled();
  await userEvent.click(screen.getByRole('checkbox', { name: /Send 1 photo to Tripo AI/ }));
  expect(generate).toBeDisabled(); // the project itself still keeps photos local
  await userEvent.click(screen.getByRole('checkbox', { name: /Allow it to use cloud providers/ }));
  expect(generate).toBeEnabled();

  await userEvent.click(generate);
  await waitFor(() => expect(api.generations.create).toHaveBeenCalled());
  expect(api.projects.update).toHaveBeenCalledWith('p1', { allow_cloud_upload: true });
  const [projectId, body, key] = vi.mocked(api.generations.create).mock.calls[0]!;
  expect(projectId).toBe('p1');
  expect(body).toMatchObject({
    inputs: [{ image_id: 'i1', role: 'source' }],
    provider: null,
    quality_mode: 'fast',
    processing_mode: 'auto',
    cloud_upload_consent: true,
  });
  expect(key).toEqual(expect.any(String));
});

it('shows why generation cannot run and links to Settings', async () => {
  const api = fakeApi({
    providers: { list: vi.fn().mockResolvedValue({ items: [] }) },
    generations: {
      plan: vi.fn().mockResolvedValue(
        plan({
          ok: false,
          provider: null,
          route: null,
          estimate: null,
          requires_consent: false,
          consent_reason: null,
          blocking_issues: [{ code: 'no_provider', message: 'No AI provider is configured.' }],
        }),
      ),
    },
  });
  renderWithStudio(<GenerationPanel project={project} images={images} />, api);
  expect(await screen.findByText('No AI provider is configured.')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: /AI providers/ })).toHaveAttribute('href', '/settings');
  expect(screen.getByRole('button', { name: 'Generate' })).toBeDisabled();
});

it('offers view slots for multi-photo projects and rejects reused photos', async () => {
  const api = fakeApi({
    providers: { list: vi.fn().mockResolvedValue(providers) },
    generations: { plan: vi.fn().mockResolvedValue(plan({ kind: 'multi_view_to_3d' })) },
  });
  renderWithStudio(
    <GenerationPanel project={{ ...project, input_mode: 'multi_view' }} images={images} />,
    api,
  );
  const left = screen.getByRole('combobox', { name: 'Left photo' });
  await userEvent.selectOptions(left, 'i1');
  expect(screen.getByText('Use each photo for one view only.')).toBeInTheDocument();
  await userEvent.selectOptions(left, 'i2');
  await waitFor(() =>
    expect(api.generations.plan).toHaveBeenLastCalledWith('p1', {
      inputs: [
        { image_id: 'i1', role: 'front' },
        { image_id: 'i2', role: 'left' },
      ],
      provider: null,
      quality_mode: 'fast',
      processing_mode: 'auto',
    }),
  );
});

it('sends a capture set to local photogrammetry without view labels', async () => {
  const many = Array.from({ length: 6 }, (_, n) => ({
    id: `c${n}`,
    original_filename: `view-${n}.jpg`,
  })) as ImageRead[];
  const api = fakeApi({
    providers: {
      list: vi.fn().mockResolvedValue({
        items: [
          ...providers.items,
          {
            key: 'local-photogrammetry',
            display_name: 'Local photogrammetry (COLMAP)',
            configured: true,
            uploads_images: false,
          },
        ],
      }),
    },
    generations: {
      plan: vi.fn().mockResolvedValue(
        plan({
          kind: 'photogrammetry',
          route: 'local',
          requires_consent: false,
          consent_reason: null,
          provenance: 'measured',
          provider: {
            key: 'local-photogrammetry',
            display_name: 'Local photogrammetry (COLMAP)',
            kind: 'local',
            uploads_images: false,
          },
          estimate: { amount: 0, unit: 'USD', usd: 0, basis: 'Runs on this computer', notes: [] },
        }),
      ),
    },
  });
  renderWithStudio(
    <GenerationPanel project={{ ...project, input_mode: 'multi_view' }} images={many} />,
    api,
  );
  // More photos than AI multi-view accepts: photogrammetry is the default.
  expect(screen.getByRole('radio', { name: /Photogrammetry/ })).toHaveAttribute(
    'aria-checked',
    'true',
  );
  expect(screen.getByText(/Uses 6 of 6 photos/)).toBeInTheDocument();
  expect(screen.queryByRole('combobox', { name: 'Provider' })).not.toBeInTheDocument();
  await waitFor(() =>
    expect(api.generations.plan).toHaveBeenLastCalledWith('p1', {
      inputs: many.map((image) => ({ image_id: image.id, role: 'source' })),
      provider: 'local-photogrammetry',
      quality_mode: 'fast',
      processing_mode: 'local',
    }),
  );

  await userEvent.click(screen.getByText('Choose photos'));
  await userEvent.click(screen.getByRole('checkbox', { name: 'view-2.jpg' }));
  expect(screen.getByText(/Uses 5 of 6 photos/)).toBeInTheDocument();

  // No consent needed: nothing is uploaded.
  expect(await screen.findByText('Estimated cost: Free')).toBeInTheDocument();
  expect(screen.queryByRole('checkbox', { name: /Send/ })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Reconstruct' })).toBeEnabled();

  // AI multi-view brings back the view slots and the provider list (cloud only).
  await userEvent.click(screen.getByRole('radio', { name: /AI multi-view/ }));
  expect(screen.getByRole('combobox', { name: 'Front photo' })).toBeInTheDocument();
  const options = screen.getAllByRole('option').map((o) => o.textContent);
  expect(options).not.toContain('Local photogrammetry (COLMAP)');
});
