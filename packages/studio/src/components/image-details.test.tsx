import type { ImageRead } from '@ai3d/shared-types';
import { screen } from '@testing-library/react';

import { renderWithStudio } from '../test/render';
import { ImageDetails } from './image-details';

const image: ImageRead = {
  id: 'img-1',
  project_id: 'p-1',
  kind: 'original',
  position: 0,
  original_filename: 'figure.jpg',
  mime_type: 'image/jpeg',
  size_bytes: 1000,
  sha256: 'abc',
  width: 3840,
  height: 2160,
  exif_orientation: null,
  camera_make: null,
  camera_model: null,
  focal_length_mm: null,
  focal_length_35mm: null,
  exif: {},
  quality_level: 'warning',
  analysis: {
    sharpness: 500,
    brightness: 0.08,
    contrast: 0.13,
    clipped_highlights: 0,
    clipped_shadows: 0.58,
    megapixels: 8.29,
    subject_coverage: null,
    warnings: [{ code: 'underexposed', severity: 'warning', message: 'Image is very dark.' }],
    cutout: null,
  },
  created_at: '2026-11-08T00:00:00Z',
  file_url: '/api/v1/images/img-1/file',
  thumbnail_url: '/api/v1/images/img-1/thumbnail',
  cutout_url: null,
};

function show(item: ImageRead) {
  renderWithStudio(
    <ImageDetails image={item} onClose={vi.fn()} onDelete={vi.fn()} deleting={false} />,
  );
}

it('shows no cut-out until it has been checked', () => {
  show(image);
  expect(screen.queryByText('What the AI will see')).not.toBeInTheDocument();
});

it('shows what the AI will see and the cut-out problems', () => {
  show({
    ...image,
    analysis: {
      ...image.analysis,
      cutout: {
        background_removed: true,
        coverage: 0.38,
        touches: ['bottom'],
        background_match: 0.89,
        checked_at: '2026-11-08T00:00:00Z',
        warnings: [
          { code: 'subject_cropped', severity: 'warning', message: 'Runs off the bottom edge.' },
          { code: 'low_edge_contrast', severity: 'warning', message: 'Blends in.' },
        ],
      },
    },
    cutout_url: '/api/v1/images/img-1/cutout?v=1',
  });
  expect(screen.getByText('What the AI will see')).toBeInTheDocument();
  expect(
    screen.getByAltText('Cut-out of figure.jpg as local AI generation receives it'),
  ).toHaveAttribute('src', 'http://api/api/v1/images/img-1/cutout?v=1');
  expect(screen.getByText('Runs off the bottom edge.')).toBeInTheDocument();
  expect(screen.getByText('Blends in.')).toBeInTheDocument();
  // The photo's own warnings are still listed separately.
  expect(screen.getByText('Image is very dark.')).toBeInTheDocument();
});
