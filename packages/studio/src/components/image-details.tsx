'use client';

import type { CutoutRead, ImageRead, QualityWarningRead } from '@ai3d/shared-types';
import { Badge, Button, ConfirmDialog } from '@ai3d/ui';
import { formatBytes, formatPercent } from '@ai3d/utilities';
import { CircleAlert, ExternalLink, Info, Trash2, TriangleAlert, X } from 'lucide-react';
import type { ReactNode } from 'react';

import { useApi } from '../context';
import { qualityLevelLabel, qualityLevelTone } from '../labels';

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1.5 text-[13px]">
      <dt className="text-subtle-foreground">{label}</dt>
      <dd className="min-w-0 truncate text-right text-foreground">{children}</dd>
    </div>
  );
}

const severityIcon = {
  error: <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-label="Problem" />,
  warning: <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-label="Warning" />,
  info: <Info className="mt-0.5 size-4 shrink-0 text-accent" aria-label="Note" />,
} satisfies Record<QualityWarningRead['severity'], ReactNode>;

function CutoutPreview({ src, name, cutout }: { src: string; name: string; cutout: CutoutRead }) {
  const problems = cutout.warnings.filter((w) => w.severity !== 'info');
  return (
    <figure aria-label="What the AI will see" className="mb-3">
      <figcaption className="mb-1.5 text-[13px] font-medium">What the AI will see</figcaption>
      {/* eslint-disable-next-line @next/next/no-img-element -- served by the backend */}
      <img
        src={src}
        alt={`Cut-out of ${name} as local AI generation receives it`}
        className="mx-auto max-h-56 rounded-md bg-surface-raised object-contain"
      />
      <p className="mt-1.5 text-xs text-subtle-foreground">
        {cutout.background_removed
          ? 'Background removed on this computer, as local generation will do it. Check that no part is missing.'
          : 'Your photo’s own transparency is used as the cut-out.'}
      </p>
      {problems.length > 0 ? (
        <ul className="mt-2 flex flex-col gap-2">
          {problems.map((warning) => (
            <li key={warning.code} className="flex gap-2 text-[13px] text-muted-foreground">
              {severityIcon[warning.severity]}
              <span>{warning.message}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </figure>
  );
}

interface ImageDetailsProps {
  image: ImageRead;
  onClose: () => void;
  onDelete: () => void;
  deleting: boolean;
}

export function ImageDetails({ image, onClose, onDelete, deleting }: ImageDetailsProps) {
  const api = useApi();
  const camera = [image.camera_make, image.camera_model].filter(Boolean).join(' ');
  const { analysis } = image;
  const tone = qualityLevelTone[image.quality_level];

  return (
    <section aria-label="Photo details" className="rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <h2 className="truncate text-[13px] font-medium" title={image.original_filename}>
          {image.original_filename}
        </h2>
        <Button
          size="icon"
          variant="ghost"
          className="size-7"
          onClick={onClose}
          aria-label="Close details"
        >
          <X />
        </Button>
      </div>
      {/* eslint-disable-next-line @next/next/no-img-element -- served by the backend */}
      <img
        src={api.url(image.thumbnail_url)}
        alt={`Preview of ${image.original_filename}`}
        className="max-h-64 w-full bg-surface-raised object-contain"
      />
      <div className="px-4 py-3">
        <div className="mb-2 flex items-center gap-2">
          <Badge variant={tone}>{qualityLevelLabel[image.quality_level]} quality</Badge>
          <span className="text-xs text-subtle-foreground">
            {image.width} × {image.height} · {analysis.megapixels} MP
          </span>
        </div>

        {analysis.warnings.length > 0 ? (
          <ul className="mb-3 flex flex-col gap-2">
            {analysis.warnings.map((warning) => (
              <li key={warning.code} className="flex gap-2 text-[13px] text-muted-foreground">
                {severityIcon[warning.severity]}
                <span>{warning.message}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mb-3 text-[13px] text-muted-foreground">
            No problems detected for reconstruction.
          </p>
        )}

        {analysis.cutout && image.cutout_url ? (
          <CutoutPreview
            src={api.url(image.cutout_url)}
            name={image.original_filename}
            cutout={analysis.cutout}
          />
        ) : null}

        <dl className="divide-y divide-border border-y border-border">
          <Row label="Format">{image.mime_type.replace('image/', '').toUpperCase()}</Row>
          <Row label="File size">{formatBytes(image.size_bytes)}</Row>
          <Row label="Camera">{camera || '—'}</Row>
          <Row label="Focal length">
            {image.focal_length_mm ? `${image.focal_length_mm} mm` : '—'}
            {image.focal_length_35mm ? ` (${image.focal_length_35mm} mm eq.)` : ''}
          </Row>
          {typeof image.exif.date_time_original === 'string' ? (
            <Row label="Captured">{image.exif.date_time_original}</Row>
          ) : null}
          <Row label="Sharpness">{analysis.sharpness.toFixed(0)}</Row>
          <Row label="Brightness">{formatPercent(analysis.brightness)}</Row>
          <Row label="Contrast">{formatPercent(analysis.contrast)}</Row>
          <Row label="Clipped highlights">{formatPercent(analysis.clipped_highlights, 1)}</Row>
          {analysis.subject_coverage != null ? (
            <Row label="Subject coverage">{formatPercent(analysis.subject_coverage)}</Row>
          ) : null}
        </dl>

        <div className="mt-3 flex gap-2">
          <Button asChild size="sm" variant="secondary" className="flex-1">
            <a href={api.url(image.file_url)} target="_blank" rel="noreferrer">
              <ExternalLink aria-hidden /> Original
            </a>
          </Button>
          <ConfirmDialog
            trigger={
              <Button
                size="sm"
                variant="ghost"
                disabled={deleting}
                className="text-danger hover:text-danger"
              >
                <Trash2 aria-hidden /> Delete
              </Button>
            }
            title="Delete this photo?"
            description="The original file and its thumbnail are removed from this project. This cannot be undone."
            confirmLabel="Delete photo"
            destructive
            onConfirm={onDelete}
          />
        </div>
      </div>
    </section>
  );
}
