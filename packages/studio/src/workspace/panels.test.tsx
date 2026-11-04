import type { ExportRead, JobRead, ModelVersionRead } from '@ai3d/shared-types';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { fakeApi, renderWithStudio } from '../test/render';
import { EnhancePanel } from './enhance-panel';
import { ExportPanel } from './export-panel';
import { QualityPanel } from './quality-panel';

const version = {
  id: 'v1',
  model_id: 'm1',
  version_number: 1,
  source: 'reconstructed',
  surface_provenance: 'measured',
  stats: { bbox_size: [5.3, 1.5, 5.2] },
  quality_report: {
    status: 'pass',
    report_version: 2,
    metrics: {
      triangle_count: 11141,
      vertex_count: 9640,
      components: 2,
      watertight: false,
      boundary_edges: 120,
      non_manifold_edges: 0,
      winding_consistent: true,
      duplicate_faces: 0,
      degenerate_triangles: 0,
      material_count: 1,
      texture_count: 1,
      texture_sizes: [[1024, 1024]],
      has_uv: true,
      uv_out_of_range: 0,
      file_bytes: 1_700_000,
      bbox_size: [5.3, 1.5, 5.2],
      reconstruction: {
        registered_images: 30,
        total_images: 30,
        sparse_points: 7217,
        mean_reprojection_error_px: 0.31,
        dense: false,
      },
      enhancement: [
        {
          code: 'ground',
          title: 'Remove ground / table',
          status: 'applied',
          detail: 'Removed 9,874 triangles.',
        },
        {
          code: 'baking',
          title: 'Texture baking',
          status: 'unavailable',
          detail: 'Needs Blender.',
        },
      ],
    },
    issues: [{ code: 'not_watertight', severity: 'info', message: 'The surface has holes.' }],
    created_at: '2026-11-03T10:00:00Z',
  },
} as unknown as ModelVersionRead;

it('shows the full quality report with units for an unscaled model', () => {
  renderWithStudio(<QualityPanel version={version} />);
  const report = screen.getByLabelText('Quality report');
  const row = (label: string) => within(report).getByText(label).nextSibling;
  expect(row('Triangles')).toHaveTextContent('11,141');
  expect(row('Open (boundary) edges')).toHaveTextContent('120');
  expect(row('Textures')).toHaveTextContent('1024×1024');
  expect(row('Scale')).toHaveTextContent('Unknown');
  expect(row('X extent')).toHaveTextContent('5.3 units');
  expect(row('Photos placed')).toHaveTextContent('30 of 30');
  expect(row('Surface type')).toHaveTextContent('From sparse points (coarse)');
  expect(within(report).getByLabelText('Unavailable')).toBeInTheDocument();
  expect(within(report).getByText('The surface has holes.')).toBeInTheDocument();
});

it('shows real sizes once the scale is known', () => {
  const scaled = {
    ...version,
    stats: { scale: { known: true, axis: 'y', length_mm: 120 } },
  } as unknown as ModelVersionRead;
  renderWithStudio(<QualityPanel version={scaled} />);
  const report = screen.getByLabelText('Quality report');
  expect(within(report).getByText('Scale').nextSibling).toHaveTextContent('Known (Y = 120 mm)');
  expect(within(report).getByText('Y extent').nextSibling).toHaveTextContent('1.50 m');
});

const queued = { id: 'j1', status: 'queued', progress: 0, stage: null } as unknown as JobRead;
const done = {
  id: 'j1',
  status: 'succeeded',
  progress: 1,
  stage: 'done',
  result_model_version_id: 'v2',
} as unknown as JobRead;

it('plans, runs and links to the enhanced version', async () => {
  const api = fakeApi({
    models: {
      enhancePlan: vi.fn().mockResolvedValue({
        quality_mode: 'high_quality',
        steps: [
          { code: 'ground', title: 'Remove ground / table', status: 'will_run', detail: '…' },
          { code: 'cleanup', title: 'Clean-up', status: 'will_run', detail: '…' },
        ],
        notes: ['The source version is kept unchanged.'],
      }),
      enhance: vi.fn().mockResolvedValue(queued),
    },
    jobs: { get: vi.fn().mockResolvedValue(done) },
  });
  const onView = vi.fn();
  renderWithStudio(
    <EnhancePanel projectId="p1" modelId="m1" version={version} dirty onView={onView} />,
    api,
  );
  expect(screen.getByText(/Unsaved edits are not included/)).toBeInTheDocument();
  // Photogrammetry scans start with ground removal on.
  expect(screen.getByRole('checkbox', { name: /Remove the ground/ })).toBeChecked();
  expect(await screen.findByText('Remove ground / table')).toBeInTheDocument();

  await userEvent.click(screen.getByRole('checkbox', { name: /Set the real-world size/ }));
  const enhance = screen.getByRole('button', { name: /Enhance \(High quality\)/i });
  expect(enhance).toBeDisabled(); // a measurement is required once chosen
  await userEvent.type(screen.getByLabelText('Length (mm)'), '120');
  await userEvent.click(enhance);

  await waitFor(() => expect(api.models.enhance).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.models.enhance).mock.calls[0]![1]).toEqual({
    quality_mode: 'high_quality',
    source_version_id: 'v1',
    remove_ground: true,
    scale: { axis: 'y', length_mm: 120 },
    smoothing: 0,
    remesh: null,
  });
  await userEvent.click(await screen.findByRole('button', { name: 'View the new version' }));
  expect(onView).toHaveBeenCalledWith(
    'v2',
    expect.stringContaining('version 1, which is unchanged'),
  );
});

it('asks for smoothing, retopology and baked maps', async () => {
  const api = fakeApi({
    models: {
      enhancePlan: vi
        .fn()
        .mockResolvedValue({ quality_mode: 'high_quality', steps: [], notes: [] }),
      enhance: vi.fn().mockResolvedValue(queued),
    },
    jobs: { get: vi.fn().mockResolvedValue(done) },
  });
  renderWithStudio(
    <EnhancePanel projectId="p1" modelId="m1" version={version} dirty={false} onView={vi.fn()} />,
    api,
  );
  await userEvent.selectOptions(screen.getByLabelText('Smoothing'), '10');
  await userEvent.selectOptions(screen.getByLabelText('Retopology'), 'quadriflow');
  expect(screen.getByText(/kept only if it stays within 1%/)).toBeInTheDocument();
  const faces = screen.getByLabelText('About how many quads');
  await userEvent.clear(faces);
  await userEvent.type(faces, '5000');
  await userEvent.click(screen.getByRole('checkbox', { name: /normal map/ }));
  await userEvent.click(screen.getByRole('button', { name: /Enhance \(High quality\)/i }));
  await waitFor(() => expect(api.models.enhance).toHaveBeenCalledTimes(1));
  expect(vi.mocked(api.models.enhance).mock.calls[0]![1]).toMatchObject({
    smoothing: 10,
    remesh: { method: 'quadriflow', target_faces: 5000, maps: ['normal'] },
  });
});

const ready: ExportRead = {
  id: 'e1',
  model_version_id: 'v1',
  format: 'stl',
  status: 'ready',
  validation: {
    reopened_with: 'trimesh',
    triangles: 11141,
    size: [5322.79, 1498.94, 5156.35],
    units: 'millimetres',
    textured: false,
  },
  notes: ['Geometry only.', "The model's real-world scale is unknown."],
  error_message: null,
  size_bytes: 557_000,
  created_at: '2026-11-03T10:00:00Z',
  completed_at: '2026-11-03T10:00:01Z',
  file_url: '/api/v1/exports/e1/file',
};

it('exports a format and lists the verified file; unavailable formats say why', async () => {
  const api = fakeApi({
    exports: {
      formats: vi.fn().mockResolvedValue({
        items: [
          {
            format: 'stl',
            label: 'STL',
            available: true,
            unavailable_reason: null,
            keeps_texture: false,
            notes: ['Geometry only.'],
          },
          {
            format: 'fbx',
            label: 'FBX',
            available: false,
            unavailable_reason: 'Needs Blender, which is not set up yet.',
            keeps_texture: true,
            notes: [],
          },
        ],
      }),
      list: vi
        .fn()
        .mockResolvedValueOnce({ items: [] })
        .mockResolvedValue({ items: [ready] }),
      create: vi.fn().mockResolvedValue(ready),
    },
  });
  renderWithStudio(<ExportPanel modelName="Mug" version={version} dirty={false} />, api);
  expect(await screen.findByText('Needs Blender, which is not set up yet.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Export as FBX' })).toBeDisabled();

  await userEvent.click(screen.getByRole('button', { name: 'Export as STL' }));
  expect(api.exports.create).toHaveBeenCalledWith('v1', 'stl');
  const files = await screen.findByRole('list', { name: 'Exported files' });
  expect(within(files).getByText('Re-opened and verified')).toBeInTheDocument();
  expect(
    within(files).getByText(/11,141 triangles · 5323 × 1499 × 5156 millimetres/),
  ).toBeInTheDocument();
  expect(within(files).getByRole('link', { name: 'Download STL' })).toHaveAttribute(
    'href',
    'http://api/api/v1/exports/e1/file',
  );
});
