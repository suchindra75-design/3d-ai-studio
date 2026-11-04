'use client';

import { Button, cn } from '@ai3d/ui';
import { IMAGE_ACCEPT_ATTRIBUTE, formatBytes } from '@ai3d/utilities';
import { ImagePlus } from 'lucide-react';
import { useId, useRef, useState, type DragEvent } from 'react';

interface DropzoneProps {
  onFiles: (files: File[]) => void;
  maxBytes: number;
  compact?: boolean;
}

/** Drag-and-drop target plus an accessible "Choose photos" button (works on phones). */
export function Dropzone({ onFiles, maxBytes, compact = false }: DropzoneProps) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const depth = useRef(0);
  const [dragging, setDragging] = useState(false);

  function hasFiles(event: DragEvent) {
    return Array.from(event.dataTransfer.types).includes('Files');
  }

  return (
    <div
      onDragEnter={(event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        depth.current += 1;
        setDragging(true);
      }}
      onDragOver={(event) => {
        if (hasFiles(event)) event.preventDefault();
      }}
      onDragLeave={() => {
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) setDragging(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        depth.current = 0;
        setDragging(false);
        const files = Array.from(event.dataTransfer.files);
        if (files.length) onFiles(files);
      }}
      className={cn(
        'flex flex-col items-center justify-center rounded-lg border border-dashed text-center transition-colors',
        compact ? 'gap-2 px-4 py-5 sm:flex-row sm:gap-4' : 'gap-3 px-6 py-12',
        dragging ? 'border-accent bg-accent-soft' : 'border-border-strong bg-surface',
      )}
    >
      <ImagePlus
        className={cn('text-muted-foreground', compact ? 'size-5' : 'size-7')}
        strokeWidth={1.5}
        aria-hidden
      />
      <div className={cn('flex flex-col gap-1', compact && 'sm:items-start sm:text-left')}>
        <p className="text-sm font-medium">{dragging ? 'Drop to upload' : 'Drag photos here'}</p>
        <p className="text-xs text-subtle-foreground">
          JPEG, PNG or WebP · up to {formatBytes(maxBytes, 0)} each
          {compact ? null : ' · originals are kept unchanged'}
        </p>
      </div>
      <input
        ref={inputRef}
        id={inputId}
        type="file"
        multiple
        accept={IMAGE_ACCEPT_ATTRIBUTE}
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          if (files.length) onFiles(files);
          event.target.value = '';
        }}
      />
      <Button
        variant={compact ? 'secondary' : 'primary'}
        size="sm"
        className={cn(compact && 'sm:ml-auto')}
        onClick={() => inputRef.current?.click()}
      >
        Choose photos
      </Button>
    </div>
  );
}
