import { render, screen } from '@testing-library/react';

import { Button } from './button';

it('defaults to type="button" so it never submits forms by accident', () => {
  render(<Button>Save</Button>);
  expect(screen.getByRole('button', { name: 'Save' })).toHaveAttribute('type', 'button');
});

it('renders its child element when asChild is set', () => {
  render(
    <Button asChild variant="primary">
      <a href="/projects">Open</a>
    </Button>,
  );
  const link = screen.getByRole('link', { name: 'Open' });
  expect(link).toHaveAttribute('href', '/projects');
  expect(link).not.toHaveAttribute('type');
  expect(link.className).toContain('bg-primary');
});
