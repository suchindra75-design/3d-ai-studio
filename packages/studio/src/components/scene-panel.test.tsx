import type { JobRead, ProjectRead, ProviderList, SceneRead } from '@ai3d/shared-types';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { fakeApi, renderWithStudio } from '../test/render';
import { SceneList } from './scene-list';
import { ScenePanel } from './scene-panel';

const project = {
  id: 'p1',
  name: 'Beach shot',
  input_mode: 'prompt',
  default_quality_mode: 'high_quality',
  default_processing_mode: 'auto',
  allow_cloud_upload: false,
} as ProjectRead;

const providers = {
  items: [
    { key: 'gpu-server', display_name: 'Your GPU server', base_url: 'http://127.0.0.1:8800' },
  ],
} as ProviderList;

const ready = { ready: true, problems: [] };

function scene(overrides: Partial<SceneRead> = {}): SceneRead {
  return {
    id: 's1',
    project_id: 'p1',
    title: 'Beach day',
    prompt: 'A red car and a palm tree on a beach at sunset',
    status: 'ready',
    job: null,
    source_scene_id: null,
    style: 'cinematic',
    lighting: 'golden_hour',
    ground: 'sand',
    shot: 'orbit',
    angle: 'eye',
    lens_mm: 35,
    seconds: 6,
    quality_mode: 'high_quality',
    objects: [
      {
        name: 'red car',
        description: 'a red car',
        size_m: 4.5,
        position_m: [0, 0],
        elevation_m: 0,
        facing_deg: 0,
        motion: 'drive',
        model_id: 'm1',
        model_version_id: 'v1',
      },
    ],
    files: [
      {
        name: 'video',
        filename: 'beach-day.mp4',
        mime_type: 'video/mp4',
        size_bytes: 2_000_000,
        url: '/api/v1/scenes/s1/files/video',
      },
      {
        name: 'still',
        filename: 'beach-day.png',
        mime_type: 'image/png',
        size_bytes: 900_000,
        url: '/api/v1/scenes/s1/files/still',
      },
    ],
    render: { frames: 144, fps: 24, width: 1920, height: 1080, engines: {}, seconds: {} },
    created_at: '2026-11-12T10:00:00Z',
    updated_at: '2026-11-12T10:05:00Z',
    ...overrides,
  };
}

it('makes a scene only after the prompt is agreed to go to your GPU server', async () => {
  const api = fakeApi({
    providers: { list: vi.fn().mockResolvedValue(providers) },
    scenes: {
      readiness: vi.fn().mockResolvedValue(ready),
      create: vi.fn().mockResolvedValue(scene({ status: 'making' })),
    },
  });
  renderWithStudio(<ScenePanel project={project} />, api);

  const make = screen.getByRole('button', { name: 'Make scene' });
  await userEvent.type(
    screen.getByLabelText('Describe the shot'),
    'A red car and a palm tree on a beach at sunset',
  );
  await userEvent.selectOptions(screen.getByLabelText('Camera move'), 'crane_up');
  await userEvent.selectOptions(screen.getByLabelText('Length'), '10');
  expect(make).toBeDisabled(); // not agreed yet
  expect(await screen.findByText('http://127.0.0.1:8800')).toBeInTheDocument();
  await userEvent.click(
    screen.getByRole('checkbox', { name: /Send this prompt to your GPU server/ }),
  );
  await waitFor(() => expect(make).toBeEnabled());
  await userEvent.click(make);

  await waitFor(() => expect(api.scenes.create).toHaveBeenCalledTimes(1));
  const [projectId, body, key] = vi.mocked(api.scenes.create).mock.calls[0]!;
  expect(projectId).toBe('p1');
  expect(body).toEqual({
    prompt: 'A red car and a palm tree on a beach at sunset',
    send_to_gpu_server: true,
    seconds: 10,
    quality_mode: 'high_quality',
    shot: 'crane_up',
    angle: 'auto',
    lighting: 'auto',
    ground: 'auto',
    style: 'auto',
  });
  expect(key).toEqual(expect.any(String));
  // Cleared for the next scene.
  await waitFor(() => expect(screen.getByLabelText('Describe the shot')).toHaveValue(''));
});

it('says what to set up before scenes can be made', async () => {
  const api = fakeApi({
    providers: { list: vi.fn().mockResolvedValue({ items: [] }) },
    scenes: {
      readiness: vi.fn().mockResolvedValue({
        ready: false,
        problems: ['Set up your GPU server in Settings → AI providers.'],
      }),
    },
  });
  renderWithStudio(<ScenePanel project={project} />, api);
  expect(await screen.findByText(/Set up your GPU server in Settings/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Open Settings' })).toHaveAttribute('href', '/settings');
  await userEvent.type(screen.getByLabelText('Describe the shot'), 'A robot');
  await userEvent.click(screen.getByRole('checkbox'));
  expect(screen.getByRole('button', { name: 'Make scene' })).toBeDisabled();
});

it('plays a finished scene and offers its files', async () => {
  renderWithStudio(<SceneList scenes={[scene()]} />);
  const video = screen.getByLabelText('Beach day: rendered shot');
  expect(video.tagName).toBe('VIDEO');
  expect(video).toHaveAttribute('src', expect.stringContaining('/api/v1/scenes/s1/files/video'));
  expect(video).toHaveAttribute('poster', expect.stringContaining('/files/still'));
  expect(screen.getByRole('link', { name: 'red car' })).toHaveAttribute(
    'href',
    '/projects/p1/models/m1',
  );
  const download = screen.getByRole('link', { name: /Download the video/ });
  expect(download).toHaveAttribute('download', 'beach-day.mp4');
  expect(download).toHaveAttribute('href', expect.stringContaining('download=true'));
  expect(screen.getByText('6 s · 1920×1080')).toBeInTheDocument();
});

it('shows progress while a scene is being made', () => {
  const job = { id: 'j1', status: 'running', stage: 'shaping', progress: 0.42 } as JobRead;
  renderWithStudio(
    <SceneList scenes={[scene({ status: 'making', job, files: [], render: null })]} />,
  );
  expect(screen.getByText('Shaping an object · 42%')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Delete the scene/ })).not.toBeInTheDocument();
});

it('renders the same objects again with other settings', async () => {
  const api = fakeApi({
    scenes: { renderAgain: vi.fn().mockResolvedValue(scene({ id: 's2', status: 'making' })) },
  });
  renderWithStudio(<SceneList scenes={[scene()]} />, api);
  await userEvent.click(screen.getByRole('button', { name: 'Render again' }));
  const form = screen.getByText(/Nothing is sent to your GPU server/).parentElement!;
  await userEvent.selectOptions(within(form).getByLabelText('Light'), 'night');
  await userEvent.click(within(form).getByRole('button', { name: 'Render' }));
  await waitFor(() => expect(api.scenes.renderAgain).toHaveBeenCalledTimes(1));
  const [sceneId, body] = vi.mocked(api.scenes.renderAgain).mock.calls[0]!;
  expect(sceneId).toBe('s1');
  expect(body).toMatchObject({ lighting: 'night', shot: 'orbit', seconds: 6, ground: 'sand' });
});

it('explains why a scene failed', () => {
  const job = {
    id: 'j1',
    status: 'failed',
    progress: 0.5,
    error: { code: 'plan_unreadable', message: 'The scene planner gave no usable plan.' },
  } as JobRead;
  renderWithStudio(
    <SceneList scenes={[scene({ status: 'failed', job, files: [], render: null })]} />,
  );
  expect(screen.getByRole('alert')).toHaveTextContent('The scene planner gave no usable plan.');
});
