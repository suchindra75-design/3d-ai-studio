import type { CaptureAssessment } from '@ai3d/shared-types';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { renderWithStudio } from '../test/render';
import { CapturePanel } from './capture-panel';

const assessment: CaptureAssessment = {
  input_mode: 'multi_view',
  image_count: 5,
  min_images: 2,
  recommended_images: 40,
  readiness: 'needs_attention',
  summary: 'Photos can be used, but some checks need attention.',
  checks: [
    {
      code: 'sharpness',
      status: 'fail',
      title: '2 blurry photos',
      message: 'Retake',
      image_ids: ['a', 'b'],
      action: null,
    },
    {
      code: 'overlap',
      status: 'not_assessed',
      title: 'Overlap not measured yet',
      message: '…',
      image_ids: [],
      action: 'measure_overlap',
    },
  ],
};

it('shows readiness, each check and links to affected photos', async () => {
  const onSelectImage = vi.fn();
  renderWithStudio(
    <CapturePanel assessment={assessment} loading={false} onSelectImage={onSelectImage} />,
  );
  expect(screen.getByText('Needs attention')).toBeInTheDocument();
  expect(screen.getByLabelText('Problem')).toBeInTheDocument();
  expect(screen.getByLabelText('Not assessed')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: 'Show 2 affected photos' }));
  expect(onSelectImage).toHaveBeenCalledWith('a');
});

it('runs the overlap measurement from its check', async () => {
  const onMeasureOverlap = vi.fn();
  const { rerender } = renderWithStudio(
    <CapturePanel
      assessment={assessment}
      loading={false}
      onSelectImage={vi.fn()}
      onMeasureOverlap={onMeasureOverlap}
    />,
  );
  await userEvent.click(screen.getByRole('button', { name: 'Measure overlap' }));
  expect(onMeasureOverlap).toHaveBeenCalledTimes(1);
  rerender(
    <CapturePanel
      assessment={assessment}
      loading={false}
      onSelectImage={vi.fn()}
      onMeasureOverlap={onMeasureOverlap}
      measuring
    />,
  );
  expect(screen.getByRole('button', { name: /Matching features/ })).toBeDisabled();
});
