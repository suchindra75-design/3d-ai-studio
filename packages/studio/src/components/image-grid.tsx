'use client';

import type { ImageRead } from '@ai3d/shared-types';
import { StatusDot, cn } from '@ai3d/ui';

import { useApi } from '../context';
import { qualityLevelLabel, qualityLevelTone } from '../labels';

interface ImageGridProps {
  images: ImageRead[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function ImageGrid({ images, selectedId, onSelect }: ImageGridProps) {
  const api = useApi();
  return (
    <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4" aria-label="Photos">
      {images.map((image) => {
        const selected = image.id === selectedId;
        return (
          <li key={image.id}>
            <button
              type="button"
              onClick={() => onSelect(image.id)}
              aria-pressed={selected}
              aria-label={`${image.original_filename}, quality ${qualityLevelLabel[image.quality_level]}`}
              className={cn(
                'group flex w-full flex-col overflow-hidden rounded-md border bg-surface text-left transition-colors',
                selected
                  ? 'border-accent ring-1 ring-accent'
                  : 'border-border hover:border-border-strong',
              )}
            >
              <span className="relative block aspect-square overflow-hidden bg-surface-raised">
                {/* eslint-disable-next-line @next/next/no-img-element -- served by the backend */}
                <img
                  src={api.url(image.thumbnail_url)}
                  alt=""
                  loading="lazy"
                  className="size-full object-cover"
                />
                <span className="absolute left-1.5 top-1.5 flex items-center gap-1 rounded-full bg-black/60 px-1.5 py-0.5 text-[10px] font-medium text-white backdrop-blur">
                  <StatusDot tone={qualityLevelTone[image.quality_level]} className="size-1.5" />
                  {qualityLevelLabel[image.quality_level]}
                </span>
              </span>
              <span className="truncate px-2 py-1.5 text-xs text-muted-foreground">
                {image.original_filename}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
