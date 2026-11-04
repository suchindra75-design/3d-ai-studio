'use client';

import { Button, Progress } from '@ai3d/ui';
import { formatBytes } from '@ai3d/utilities';
import { CheckCircle2, CircleAlert, Copy, Loader2 } from 'lucide-react';
import { useShallow } from 'zustand/react/shallow';

import { useUploadStore, type UploadItem } from '../uploads/store';

function StatusIcon({ item }: { item: UploadItem }) {
  switch (item.status) {
    case 'done':
      return <CheckCircle2 className="size-4 text-success" aria-label="Uploaded" />;
    case 'duplicate':
      return <Copy className="size-4 text-muted-foreground" aria-label="Already in project" />;
    case 'error':
      return <CircleAlert className="size-4 text-danger" aria-label="Failed" />;
    default:
      return (
        <Loader2 className="size-4 animate-spin text-muted-foreground" aria-label="Uploading" />
      );
  }
}

function describe(item: UploadItem): string {
  switch (item.status) {
    case 'queued':
      return 'Waiting…';
    case 'uploading':
      return item.progress >= 1 ? 'Analysing…' : `Uploading ${Math.round(item.progress * 100)}%`;
    case 'done':
      return 'Uploaded and analysed';
    case 'duplicate':
      return 'Already in this project — skipped';
    case 'error':
      return item.error ?? 'Failed';
  }
}

export function UploadQueue({ projectId }: { projectId: string }) {
  const items = useUploadStore(
    useShallow((state) => state.items.filter((item) => item.projectId === projectId)),
  );
  const clearFinished = useUploadStore((state) => state.clearFinished);
  if (items.length === 0) return null;
  const active = items.some((item) => item.status === 'queued' || item.status === 'uploading');

  return (
    <section aria-label="Uploads" className="rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h2 className="text-[13px] font-medium">
          {active ? 'Uploading' : 'Uploads'}{' '}
          <span className="text-subtle-foreground">({items.length})</span>
        </h2>
        {!active ? (
          <Button size="sm" variant="ghost" onClick={() => clearFinished(projectId)}>
            Clear
          </Button>
        ) : null}
      </div>
      <ul className="max-h-56 divide-y divide-border overflow-y-auto" aria-live="polite">
        {items.map((item) => (
          <li key={item.id} className="flex items-center gap-3 px-4 py-2">
            <StatusIcon item={item} />
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-[13px]">{item.name}</span>
                <span className="shrink-0 text-xs text-subtle-foreground">
                  {formatBytes(item.size)}
                </span>
              </div>
              <p
                className={
                  item.status === 'error' ? 'text-xs text-danger' : 'text-xs text-subtle-foreground'
                }
              >
                {describe(item)}
              </p>
              {item.status === 'uploading' && item.progress < 1 ? (
                <Progress
                  value={item.progress * 100}
                  className="mt-1.5"
                  aria-label={`Uploading ${item.name}`}
                />
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
