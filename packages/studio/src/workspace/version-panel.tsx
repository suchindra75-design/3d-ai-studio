'use client';

import type { ModelVersionRead } from '@ai3d/shared-types';
import { Badge, Button, cn } from '@ai3d/ui';
import { formatBytes, formatDateTime } from '@ai3d/utilities';
import { Download } from 'lucide-react';

import { DownloadButton, versionFileName } from '../components/download-button';
import { versionSourceLabel } from '../labels';

/** Every version of the model, newest first. Versions are never overwritten. */
export function VersionPanel({
  modelName,
  versions,
  currentId,
  viewingId,
  onView,
  onMakeCurrent,
  onCompare,
  comparingId,
  busy,
}: {
  modelName: string;
  versions: ModelVersionRead[];
  currentId: string | null;
  viewingId: string;
  onView: (id: string) => void;
  onMakeCurrent: (id: string) => void;
  /** Shows a version side by side with the one being viewed. */
  onCompare: (id: string | null) => void;
  comparingId: string | null;
  busy: boolean;
}) {
  return (
    <section aria-labelledby="versions-heading" className="flex flex-col gap-3 px-4 py-4">
      <div>
        <h2 id="versions-heading" className="text-xs font-medium">
          Versions
        </h2>
        <p className="mt-1 text-xs text-subtle-foreground">
          Saving edits adds a version; earlier versions are kept unchanged. The current version is
          the one the project shows and downloads by default.
        </p>
      </div>
      <ol className="flex flex-col gap-2" aria-label="Version history">
        {versions.map((version) => {
          const viewing = version.id === viewingId;
          const current = version.id === currentId;
          return (
            <li
              key={version.id}
              className={cn(
                'flex flex-col gap-2 rounded-md border p-3',
                viewing ? 'border-accent/60 bg-accent-soft' : 'border-border bg-background',
              )}
            >
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-xs font-medium">Version {version.version_number}</span>
                <Badge variant="neutral">{versionSourceLabel[version.source]}</Badge>
                {current ? <Badge variant="success">Current</Badge> : null}
                {viewing ? <Badge variant="accent">Viewing</Badge> : null}
              </div>
              <p className="text-[11px] text-subtle-foreground">
                {formatDateTime(version.created_at)}
                {version.mesh ? ` · ${formatBytes(version.mesh.size_bytes)}` : ''}
              </p>
              <p className="text-xs leading-relaxed text-muted-foreground">{version.notes}</p>
              <div className="flex flex-wrap gap-1.5">
                {!viewing ? (
                  <Button size="sm" variant="secondary" onClick={() => onView(version.id)}>
                    View
                  </Button>
                ) : null}
                {!viewing ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    aria-pressed={comparingId === version.id}
                    onClick={() => onCompare(comparingId === version.id ? null : version.id)}
                  >
                    {comparingId === version.id ? 'Stop comparing' : 'Compare'}
                  </Button>
                ) : null}
                {!current ? (
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={busy}
                    onClick={() => onMakeCurrent(version.id)}
                  >
                    Make current
                  </Button>
                ) : null}
                {version.file_url ? (
                  <DownloadButton
                    path={version.file_url}
                    fileName={versionFileName(modelName, version.version_number, 'glb')}
                    label={`Download version ${version.version_number} as GLB`}
                    variant="ghost"
                  >
                    <Download aria-hidden /> GLB
                  </DownloadButton>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
