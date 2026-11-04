'use client';

import type { ModelRead } from '@ai3d/shared-types';
import { Badge, Button, ConfirmDialog } from '@ai3d/ui';
import { formatBytes } from '@ai3d/utilities';
import { Box, Download, Rotate3d, Trash2 } from 'lucide-react';
import { useState } from 'react';

import { useDeleteModel } from '../api/hooks';
import { DownloadButton, versionFileName } from './download-button';
import { useApi, useStudio } from '../context';
import { routes } from '../routes';
import { provenanceLabel, qualityModeLabel, reportStatusLabel, reportStatusTone } from '../labels';

function stat(value: unknown): string {
  return typeof value === 'number' ? value.toLocaleString() : '—';
}

function ModelCard({ model, projectId }: { model: ModelRead; projectId: string }) {
  const api = useApi();
  const { Link } = useStudio().navigation;
  const remove = useDeleteModel(projectId);
  const [confirming, setConfirming] = useState(false);
  const version = model.current_version;
  if (!version) return null;
  const stats = version.stats as Record<string, unknown>;
  const size = Array.isArray(stats.bbox_size) ? (stats.bbox_size as number[]) : null;
  const report = version.quality_report;
  const issues = report?.issues.filter((issue) => issue.severity !== 'info') ?? [];
  const notes = report?.issues.filter((issue) => issue.severity === 'info') ?? [];

  return (
    <li className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-3.5 sm:flex-row">
      <div className="flex aspect-square w-full shrink-0 items-center justify-center overflow-hidden rounded-md bg-surface-hover sm:w-32">
        {version.preview_url ? (
          // eslint-disable-next-line @next/next/no-img-element -- served by the backend
          <img
            src={api.url(version.preview_url)}
            alt={`Preview of ${model.name} rendered by the provider`}
            className="size-full object-cover"
          />
        ) : (
          <Box className="size-8 text-subtle-foreground" aria-hidden />
        )}
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate text-[13px] font-medium">{model.name}</p>
            <p className="text-xs text-subtle-foreground">
              {version.quality_mode ? qualityModeLabel[version.quality_mode] : null}
              {version.mesh ? ` · GLB · ${formatBytes(version.mesh.size_bytes)}` : null}
              {model.version_count > 1
                ? ` · version ${version.version_number} of ${model.version_count}`
                : null}
            </p>
          </div>
          <div className="flex gap-1.5">
            <Button asChild size="sm" variant="primary">
              <Link
                href={routes.model(projectId, model.id)}
                aria-label={`Open ${model.name} in 3D`}
              >
                <Rotate3d aria-hidden /> Open
              </Link>
            </Button>
            {version.file_url ? (
              <DownloadButton
                path={version.file_url}
                fileName={versionFileName(model.name, version.version_number, 'glb')}
                label={`Download ${model.name} as GLB`}
              >
                <Download aria-hidden /> GLB
              </DownloadButton>
            ) : null}
            <Button
              size="icon"
              variant="ghost"
              aria-label={`Delete ${model.name}`}
              onClick={() => setConfirming(true)}
            >
              <Trash2 aria-hidden />
            </Button>
          </div>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Badge variant={version.surface_provenance === 'measured' ? 'success' : 'warning'}>
            {provenanceLabel[version.surface_provenance]}
          </Badge>
          {report ? (
            <Badge variant={reportStatusTone[report.status]}>
              {reportStatusLabel[report.status]}
            </Badge>
          ) : null}
        </div>
        <p className="text-xs leading-relaxed text-muted-foreground">{version.notes}</p>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
          <div>
            <dt className="text-subtle-foreground">Triangles</dt>
            <dd>{stat(stats.triangle_count)}</dd>
          </div>
          <div>
            <dt className="text-subtle-foreground">Vertices</dt>
            <dd>{stat(stats.vertex_count)}</dd>
          </div>
          <div>
            <dt className="text-subtle-foreground">Textures</dt>
            <dd>{stat(stats.texture_count)}</dd>
          </div>
          <div>
            <dt className="text-subtle-foreground">Bounding box</dt>
            <dd title="In the model's own units; the real-world scale is unknown">
              {size ? size.map((v) => v.toFixed(2)).join(' × ') : '—'}
            </dd>
          </div>
        </dl>
        {issues.length + notes.length > 0 ? (
          <ul className="flex flex-col gap-0.5 text-xs">
            {issues.map((issue) => (
              <li key={issue.code} className="text-warning">
                {issue.message}
              </li>
            ))}
            {notes.map((issue) => (
              <li key={issue.code} className="text-subtle-foreground">
                {issue.message}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Delete “${model.name}”?`}
        description="This permanently deletes the model file from this machine. The generation history is kept."
        confirmLabel="Delete model"
        destructive
        onConfirm={() => remove.mutate(model.id)}
      />
    </li>
  );
}

export function ModelList({ models, projectId }: { models: ModelRead[]; projectId: string }) {
  if (models.length === 0) return null;
  return (
    <section aria-labelledby="models-heading" className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between">
        <h2 id="models-heading" className="text-sm font-medium">
          3D models
        </h2>
        <span className="text-xs text-subtle-foreground">
          Open a model to inspect and edit it in 3D; downloads work in any 3D viewer.
        </span>
      </div>
      <ul className="flex flex-col gap-2">
        {models.map((model) => (
          <ModelCard key={model.id} model={model} projectId={projectId} />
        ))}
      </ul>
    </section>
  );
}
