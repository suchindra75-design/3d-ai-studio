'use client';

import type { ExportFormat, ExportRead, ModelVersionRead } from '@ai3d/shared-types';
import { Badge, Button, Spinner } from '@ai3d/ui';
import { formatBytes, formatDateTime } from '@ai3d/utilities';
import { formatCount } from '@ai3d/viewer';
import { Download, Trash2 } from 'lucide-react';

import { useCreateExport, useDeleteExport, useExportFormats, useExports } from '../api/hooks';
import { DownloadButton, versionFileName } from '../components/download-button';
import { ErrorState } from '../components/page';

/** Matches the backend's download name; glTF and OBJ come as zip archives. */
function exportFileName(modelName: string, versionNumber: number, format: ExportFormat): string {
  const zipped = format === 'gltf' || format === 'obj';
  const name = versionFileName(modelName, versionNumber, zipped ? 'zip' : format);
  return zipped ? name.replace(/\.zip$/, `-${format}.zip`) : name;
}

function ExportRow({
  item,
  label,
  fileName,
}: {
  item: ExportRead;
  label: string;
  fileName: string;
}) {
  const remove = useDeleteExport(item.model_version_id);
  const v = item.validation as Record<string, unknown> | null;
  return (
    <li className="flex flex-col gap-1.5 rounded-md border border-border bg-background p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs font-medium">{label}</span>
        {item.status === 'ready' ? (
          <Badge variant="success">Re-opened and verified</Badge>
        ) : item.status === 'failed' ? (
          <Badge variant="danger">Failed</Badge>
        ) : (
          <Badge>{item.status}</Badge>
        )}
        <span className="ml-auto text-[11px] text-subtle-foreground">
          {formatDateTime(item.created_at)}
        </span>
      </div>
      {v ? (
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          {formatCount(Number(v.triangles))} triangles ·{' '}
          {Number(v.quads) > 0 ? `${formatCount(Number(v.quads))} quads in the file · ` : ''}
          {(v.size as number[]).map((n) => Number(n.toPrecision(4))).join(' × ')} {String(v.units)}
          {v.textured ? ' · textured' : ''} · re-opened with {String(v.reopened_with)}
        </p>
      ) : null}
      {item.error_message ? <p className="text-xs text-danger">{item.error_message}</p> : null}
      {item.notes.map((note) => (
        <p key={note} className="text-[11px] text-subtle-foreground">
          {note}
        </p>
      ))}
      <div className="flex gap-1.5">
        {item.file_url ? (
          <DownloadButton path={item.file_url} fileName={fileName} label={`Download ${label}`}>
            <Download aria-hidden /> Download
            {item.size_bytes ? ` (${formatBytes(item.size_bytes)})` : ''}
          </DownloadButton>
        ) : null}
        <Button
          size="sm"
          variant="ghost"
          aria-label={`Delete the ${label} export`}
          disabled={remove.isPending}
          onClick={() => remove.mutate(item.id)}
        >
          <Trash2 aria-hidden />
        </Button>
      </div>
    </li>
  );
}

/** Export center for the viewed version: every format, checked by re-opening the file. */
export function ExportPanel({
  modelName,
  version,
  dirty,
}: {
  modelName: string;
  version: ModelVersionRead;
  dirty: boolean;
}) {
  const formats = useExportFormats();
  const exports = useExports(version.id);
  const create = useCreateExport(version.id);
  const labels = Object.fromEntries(
    (formats.data?.items ?? []).map((f) => [f.format, f.label]),
  ) as Record<ExportFormat, string>;

  return (
    <section aria-labelledby="export-heading" className="flex flex-col gap-4 px-4 py-4">
      <div>
        <h2 id="export-heading" className="text-xs font-medium">
          Export version {version.version_number}
        </h2>
        <p className="mt-1 text-xs leading-relaxed text-subtle-foreground">
          Each file is re-opened after it is written and compared with the model (triangles, size,
          texture). A file that does not match is never offered.
        </p>
        {dirty ? (
          <p className="mt-1 text-xs text-warning">
            Unsaved edits are not included. Save them as a version first.
          </p>
        ) : null}
      </div>

      {formats.isError ? <ErrorState error={formats.error} /> : null}
      <ul className="flex flex-col gap-2" aria-label="Export formats">
        {(formats.data?.items ?? []).map((format) => {
          const busy = create.isPending && create.variables === format.format;
          return (
            <li key={format.format} className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-xs font-medium">{format.label}</p>
                <p className="text-[11px] leading-relaxed text-subtle-foreground">
                  {format.available ? format.notes.join(' ') : format.unavailable_reason}
                </p>
              </div>
              <Button
                size="sm"
                variant="secondary"
                className="shrink-0"
                disabled={!format.available || create.isPending}
                aria-label={`Export as ${format.label}`}
                onClick={() => create.mutate(format.format)}
              >
                {busy ? <Spinner label="Exporting" /> : null}
                {format.available ? (busy ? 'Exporting…' : 'Export') : 'Unavailable'}
              </Button>
            </li>
          );
        })}
      </ul>
      {create.isError ? <ErrorState error={create.error} /> : null}

      {exports.data && exports.data.items.length > 0 ? (
        <div className="flex flex-col gap-2">
          <h3 className="text-xs font-medium">Files</h3>
          <ul className="flex flex-col gap-2" aria-label="Exported files">
            {exports.data.items.map((item) => (
              <ExportRow
                key={item.id}
                item={item}
                label={labels[item.format] ?? item.format}
                fileName={exportFileName(modelName, version.version_number, item.format)}
              />
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
