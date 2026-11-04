import type { InputMode } from '@ai3d/shared-types';
import { ChevronDown } from 'lucide-react';

const MULTI_VIEW_STEPS = [
  'Walk around the object in three rings — low (≈15°), eye level (≈35°) and high (≈60°) — taking a photo every 20–30° (12–16 per ring). Add a few top-down shots.',
  'Keep 60–80% overlap between neighbouring photos. Move yourself, not the object.',
  'Lock focus distance and zoom: use one focal length for the whole capture.',
  'Use soft, even light (overcast daylight or a light tent). Avoid flash and harsh shadows; keep exposure constant.',
  'Fill most of the frame with the object and keep it sharp. Retake blurry shots.',
  'Shiny, transparent and plain single-colour surfaces reconstruct poorly. A textured background or matte scanning spray helps.',
  'Nothing in the scene should move between photos (people, pets, foliage, the object itself).',
];

const SINGLE_IMAGE_STEPS = [
  'Frame the whole object with a little margin around it.',
  'A three-quarter view (front, one side and a bit of the top) gives the model the most information.',
  'Use a plain background that contrasts with the object.',
  'Even, diffuse lighting; avoid strong reflections and deep shadows.',
  'Step back and zoom in slightly to reduce perspective distortion.',
];

export function CaptureGuide({ mode }: { mode: InputMode }) {
  const steps = mode === 'multi_view' ? MULTI_VIEW_STEPS : SINGLE_IMAGE_STEPS;
  return (
    <details className="group rounded-lg border border-border bg-surface">
      <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-2.5 text-[13px] font-medium [&::-webkit-details-marker]:hidden">
        {mode === 'multi_view'
          ? 'How to photograph an object for 3D'
          : 'Tips for a good source photo'}
        <ChevronDown
          className="size-4 text-subtle-foreground transition-transform group-open:rotate-180"
          aria-hidden
        />
      </summary>
      <ol className="flex list-decimal flex-col gap-2 border-t border-border py-3 pl-9 pr-4 text-[13px] leading-relaxed text-muted-foreground marker:text-subtle-foreground">
        {steps.map((step) => (
          <li key={step}>{step}</li>
        ))}
      </ol>
    </details>
  );
}
