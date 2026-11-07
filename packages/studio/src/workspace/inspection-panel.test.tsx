import type { FindingRead, InspectionRead, JobRead, ModelVersionRead } from '@ai3d/shared-types';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { fakeApi, renderWithStudio } from '../test/render';
import { InspectionPanel } from './inspection-panel';

const version = { id: 'v2', version_number: 2 } as unknown as ModelVersionRead;

function finding(over: Partial<FindingRead>): FindingRead {
  return {
    code: 'duplicate_faces',
    category: 'integrity',
    severity: 'warning',
    title: 'Duplicate triangles',
    count: 40,
    unit: 'triangles',
    explanation: 'Triangles that repeat another’s three corners.',
    method: 'exact',
    repair: 'remove_duplicate_faces',
    repair_risk: 'Removes the repeats.',
    meshes: ['Body'],
    locations: {
      points: [],
      segments: [],
      triangles: [[0, 0, 0, 1, 0, 0, 0, 1, 0]],
      shown: 1,
      total: 40,
    },
    details: {},
    ...over,
  };
}

const ready: InspectionRead = {
  version_id: 'v2',
  status: 'ready',
  message: null,
  created_at: '2026-11-08T10:00:00Z',
  json_url: '/api/v1/model-versions/v2/inspection/report.json',
  markdown_url: '/api/v1/model-versions/v2/inspection/report.md',
  report: {
    report_version: 1,
    triangles: 1000,
    vertices: 600,
    meshes: 1,
    seconds: 1.2,
    options: { printability: false },
    measurements: {},
    findings: [
      finding({}),
      finding({
        code: 'components',
        category: 'topology',
        severity: 'info',
        title: 'Separate pieces',
        count: 3,
        unit: 'pieces',
        method: 'exact',
        repair: 'separate_components',
        locations: { points: [], segments: [], triangles: [], shown: 0, total: 0 },
        details: {
          pieces: [
            { id: 0, triangles: 900, closed: true },
            { id: 4, triangles: 60, closed: false },
            { id: 7, triangles: 40, closed: false },
          ],
        },
      }),
    ],
    skipped: [
      {
        code: 'self_intersections',
        category: 'integrity',
        title: 'Self-intersections',
        reason: 'Not checked: over 2,000,000 triangles.',
      },
    ],
  },
};

function setup() {
  const api = fakeApi({
    inspection: { get: vi.fn().mockResolvedValue(ready), start: vi.fn() },
    repairs: { create: vi.fn().mockResolvedValue({ id: 'job1', status: 'queued' } as JobRead) },
    jobs: { get: vi.fn().mockResolvedValue({ id: 'job1', status: 'queued', progress: 0 }) },
  });
  const onShow = vi.fn();
  const onLocate = vi.fn();
  renderWithStudio(
    <InspectionPanel
      projectId="p1"
      modelId="m1"
      version={version}
      shownCode={null}
      onShow={onShow}
      onLocate={onLocate}
      onView={vi.fn()}
    />,
    api,
  );
  return { api, onShow, onLocate };
}

test('findings are grouped by category, with how they were measured and no score', async () => {
  setup();
  const integrity = await screen.findByRole('list', { name: 'Geometry integrity' });
  expect(within(integrity).getByText('Duplicate triangles')).toBeInTheDocument();
  expect(within(integrity).getByText('Exact')).toBeInTheDocument();
  expect(screen.getByText(/Showing 1 of 40 on the model/)).toBeInTheDocument();
  expect(screen.getByText(/No overall score/)).toBeInTheDocument();
  expect(screen.getByText(/Self-intersections: Not checked/)).toBeInTheDocument();
  // Printing checks were not asked for: no fabrication section.
  expect(screen.queryByText('Fabrication suitability')).not.toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'JSON' })).toHaveAttribute(
    'href',
    'http://api/api/v1/model-versions/v2/inspection/report.json',
  );
});

test('a finding can be shown and located on the model', async () => {
  const { onShow, onLocate } = setup();
  await userEvent.click(await screen.findByRole('button', { name: 'Show on model' }));
  expect(onShow).toHaveBeenCalledWith(expect.objectContaining({ code: 'duplicate_faces' }));
  await userEvent.click(screen.getByRole('button', { name: 'Locate' }));
  expect(onLocate).toHaveBeenCalledWith(expect.objectContaining({ code: 'duplicate_faces' }));
});

test('a repair says what it does and that it makes a new version, then runs only when confirmed', async () => {
  const { api } = setup();
  await userEvent.click(await screen.findByRole('button', { name: 'Remove duplicate triangles…' }));
  const dialog = await screen.findByRole('alertdialog');
  expect(within(dialog).getByText(/new version; version 2 is not changed/)).toBeInTheDocument();
  expect(api.repairs.create).not.toHaveBeenCalled();
  await userEvent.click(within(dialog).getByRole('button', { name: 'Repair' }));
  await waitFor(() =>
    expect(api.repairs.create).toHaveBeenCalledWith(
      'm1',
      { source_version_id: 'v2', operations: [{ code: 'remove_duplicate_faces' }] },
      expect.any(String),
    ),
  );
});

test('removing pieces needs them chosen, and is confirmed as destructive', async () => {
  const { api } = setup();
  await userEvent.click(await screen.findByText(/Choose pieces to remove/));
  const remove = screen.getByRole('button', { name: /Remove 0 chosen pieces/ });
  expect(remove).toBeDisabled();
  await userEvent.click(screen.getByRole('checkbox', { name: /Piece 7/ }));
  await userEvent.click(screen.getByRole('button', { name: /Remove 1 chosen piece…/ }));
  const dialog = await screen.findByRole('alertdialog');
  expect(within(dialog).getByText(/removes geometry you can see/)).toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole('button', { name: 'Remove pieces' }));
  await waitFor(() =>
    expect(api.repairs.create).toHaveBeenCalledWith(
      'm1',
      { source_version_id: 'v2', operations: [{ code: 'remove_components', pieces: [7] }] },
      expect.any(String),
    ),
  );
});
