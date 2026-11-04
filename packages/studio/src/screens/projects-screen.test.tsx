import { ApiError } from '@ai3d/api-client';
import { screen } from '@testing-library/react';

import { fakeApi, renderWithStudio } from '../test/render';
import { ProjectsScreen } from './projects-screen';

const project = {
  id: 'p1',
  name: 'Ceramic vase',
  description: '',
  status: 'active',
  input_mode: 'multi_view',
  default_quality_mode: 'fast',
  default_processing_mode: 'auto',
  allow_cloud_upload: false,
  image_count: 3,
  total_image_bytes: 3 * 1024 * 1024,
  cover_image_id: 'img1',
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  archived_at: null,
};

describe('ProjectsScreen', () => {
  it('shows an onboarding empty state', async () => {
    const api = fakeApi({
      projects: { list: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 }) },
    });
    renderWithStudio(<ProjectsScreen />, api);
    expect(await screen.findByText('Create your first project')).toBeInTheDocument();
    expect(api.projects.list).toHaveBeenCalledWith({ status: 'active', q: undefined, limit: 100 });
  });

  it('renders project cards linking to the workspace', async () => {
    const api = fakeApi({
      projects: {
        list: vi.fn().mockResolvedValue({ items: [project], total: 1, limit: 100, offset: 0 }),
      },
    });
    renderWithStudio(<ProjectsScreen />, api);
    const card = await screen.findByRole('link', { name: /Ceramic vase/ });
    expect(card).toHaveAttribute('href', '/projects/p1');
    expect(card).toHaveTextContent('Multiple photos');
    expect(card).toHaveTextContent('3 photos');
    expect(card.querySelector('img')).toHaveAttribute(
      'src',
      'http://api/api/v1/images/img1/thumbnail',
    );
  });

  it('explains how to start the backend when it is unreachable', async () => {
    const api = fakeApi({
      projects: {
        list: vi
          .fn()
          .mockRejectedValue(new ApiError(0, { code: 'network_error', message: 'Cannot reach' })),
      },
    });
    renderWithStudio(<ProjectsScreen />, api);
    expect(await screen.findByRole('alert')).toHaveTextContent('Backend not reachable');
    expect(screen.getByText('pnpm dev:backend')).toBeInTheDocument();
  });
});
