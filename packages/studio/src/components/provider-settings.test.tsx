import type { ProviderRead } from '@ai3d/shared-types';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { fakeApi, renderWithStudio } from '../test/render';
import { ProviderSettings } from './provider-settings';

const gpuServer: ProviderRead = {
  key: 'gpu-server',
  display_name: 'Your GPU server (TRELLIS)',
  kind: 'cloud_api',
  description: 'Generates on an NVIDIA GPU machine you run.',
  uploads_images: true,
  homepage_url: null,
  docs_url: null,
  pricing_url: null,
  requires_api_key: true,
  configured: false,
  credential_source: 'none',
  api_key_hint: null,
  api_key_env_var: 'AI3D_GPU_SERVER_TOKEN',
  credential_error: null,
  unavailable_reason: null,
  requires_base_url: true,
  base_url: null,
  base_url_source: 'none',
  base_url_env_var: 'AI3D_GPU_SERVER_URL',
  capabilities: {
    single_image: true,
    multi_view: { min_images: 2, max_images: 4, required_roles: ['front'] },
    accepted_mime_types: ['image/jpeg'],
    max_input_bytes: 1,
    output_formats: ['glb'],
    supports_texture: true,
    supports_pbr: false,
    cancel: 'supported',
    reports_progress: true,
    notes: [],
    photogrammetry: null,
    retexture: false,
    retexture_own_models_only: false,
  },
};

test('your GPU server asks for its address and an access token', async () => {
  const api = fakeApi({
    providers: {
      list: vi.fn().mockResolvedValue({ items: [gpuServer] }),
      setBaseUrl: vi.fn().mockResolvedValue({
        ...gpuServer,
        base_url: 'https://gpu.example.com:8800',
        base_url_source: 'stored',
      }),
    },
  });
  renderWithStudio(<ProviderSettings />, api);

  expect(await screen.findByText('Your server')).toBeInTheDocument();
  expect(screen.getByText('AI3D_GPU_SERVER_URL')).toBeInTheDocument();
  expect(screen.getByLabelText('Access token')).toHaveAttribute('type', 'password');

  await userEvent.type(screen.getByLabelText('Server address'), 'https://gpu.example.com:8800');
  await userEvent.click(screen.getByRole('button', { name: 'Save address' }));
  await waitFor(() =>
    expect(api.providers.setBaseUrl).toHaveBeenCalledWith(
      'gpu-server',
      'https://gpu.example.com:8800',
    ),
  );
});

test('a stored address is shown and can be removed', async () => {
  const api = fakeApi({
    providers: {
      list: vi.fn().mockResolvedValue({
        items: [
          {
            ...gpuServer,
            configured: true,
            credential_source: 'stored',
            api_key_hint: '…abcd',
            base_url: 'https://gpu.example.com:8800',
            base_url_source: 'stored',
          },
        ],
      }),
      deleteBaseUrl: vi.fn().mockResolvedValue(gpuServer),
    },
  });
  renderWithStudio(<ProviderSettings />, api);

  expect(await screen.findByText('https://gpu.example.com:8800')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Check connection' })).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Remove address' }));
  await waitFor(() => expect(api.providers.deleteBaseUrl).toHaveBeenCalledWith('gpu-server'));
});
