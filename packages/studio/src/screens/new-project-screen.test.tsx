import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { fakeApi, renderWithStudio } from '../test/render';
import { NewProjectScreen } from './new-project-screen';

describe('NewProjectScreen', () => {
  it('requires a name', async () => {
    const user = userEvent.setup();
    const { api } = renderWithStudio(<NewProjectScreen />);
    await user.click(screen.getByRole('button', { name: 'Create project' }));
    expect(screen.getByText('Give the project a name.')).toBeInTheDocument();
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true');
    expect(api.projects.create).not.toHaveBeenCalled();
  });

  it('creates the project with the chosen settings and opens it', async () => {
    const user = userEvent.setup();
    const create = vi.fn().mockResolvedValue({ id: 'new-id' });
    const { navigation } = renderWithStudio(
      <NewProjectScreen />,
      fakeApi({ projects: { create } }),
    );

    await user.type(screen.getByLabelText('Name'), '  Bronze statue ');
    await user.click(screen.getByRole('radio', { name: /Multiple photos/ }));
    await user.click(screen.getByRole('radio', { name: /Professional/ }));
    await user.click(screen.getByRole('switch'));
    await user.click(screen.getByRole('button', { name: 'Create project' }));

    expect(create).toHaveBeenCalledWith({
      name: 'Bronze statue',
      description: '',
      input_mode: 'multi_view',
      default_quality_mode: 'professional',
      default_processing_mode: 'auto',
      allow_cloud_upload: true,
    });
    await vi.waitFor(() => expect(navigation.replace).toHaveBeenCalledWith('/projects/new-id'));
  });

  it('never allows cloud upload for local-only processing', async () => {
    const user = userEvent.setup();
    const create = vi.fn().mockResolvedValue({ id: 'x' });
    renderWithStudio(<NewProjectScreen />, fakeApi({ projects: { create } }));

    await user.type(screen.getByLabelText('Name'), 'Private');
    await user.click(screen.getByRole('switch'));
    await user.click(screen.getByRole('radio', { name: /Local only/ }));
    expect(screen.getByRole('switch')).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Create project' }));

    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({ default_processing_mode: 'local', allow_cloud_upload: false }),
    );
  });
});
