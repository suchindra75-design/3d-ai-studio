'use client';

import type { CaptureAssessment, CaptureCheck } from '@ai3d/shared-types';
import { Badge, Button, Skeleton, Spinner } from '@ai3d/ui';
import { pluralize } from '@ai3d/utilities';
import { CircleCheck, CircleDashed, CircleX, TriangleAlert } from 'lucide-react';
import type { ReactNode } from 'react';

import { ErrorState } from './page';

const checkIcon: Record<CaptureCheck['status'], ReactNode> = {
  pass: <CircleCheck className="size-4 text-success" aria-label="Pass" />,
  warning: <TriangleAlert className="size-4 text-warning" aria-label="Warning" />,
  fail: <CircleX className="size-4 text-danger" aria-label="Problem" />,
  not_assessed: (
    <CircleDashed className="size-4 text-subtle-foreground" aria-label="Not assessed" />
  ),
};

const readiness = {
  ready: { label: 'Ready', variant: 'success' },
  needs_attention: { label: 'Needs attention', variant: 'warning' },
  insufficient: { label: 'Not enough photos', variant: 'danger' },
} as const;

interface CapturePanelProps {
  assessment: CaptureAssessment | undefined;
  loading: boolean;
  onSelectImage: (id: string) => void;
  onMeasureOverlap?: () => void;
  measuring?: boolean;
  measureError?: unknown;
}

export function CapturePanel({
  assessment,
  loading,
  onSelectImage,
  onMeasureOverlap,
  measuring = false,
  measureError = null,
}: CapturePanelProps) {
  return (
    <section aria-label="Capture assessment" className="rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2.5">
        <h2 className="text-[13px] font-medium">Capture check</h2>
        {assessment ? (
          <Badge variant={readiness[assessment.readiness].variant}>
            {readiness[assessment.readiness].label}
          </Badge>
        ) : null}
      </div>
      {loading || !assessment ? (
        <div className="flex flex-col gap-2 p-4">
          <Skeleton className="h-4 w-3/4" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {assessment.checks.map((check) => (
            <li key={check.code} className="flex gap-3 px-4 py-3">
              <span className="mt-0.5 shrink-0">{checkIcon[check.status]}</span>
              <div className="min-w-0">
                <p className="text-[13px] font-medium">{check.title}</p>
                <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                  {check.message}
                </p>
                {check.image_ids && check.image_ids.length > 0 ? (
                  <button
                    type="button"
                    className="mt-1 text-xs text-accent hover:underline"
                    onClick={() => onSelectImage(check.image_ids![0]!)}
                  >
                    Show {pluralize(check.image_ids.length, 'affected photo')}
                  </button>
                ) : null}
                {check.action === 'measure_overlap' && onMeasureOverlap ? (
                  <div className="mt-2 flex flex-col gap-2">
                    <Button
                      size="sm"
                      variant="secondary"
                      className="self-start"
                      disabled={measuring}
                      onClick={onMeasureOverlap}
                    >
                      {measuring ? <Spinner label="Measuring overlap" /> : null}
                      {measuring
                        ? 'Matching features…'
                        : check.status === 'not_assessed'
                          ? 'Measure overlap'
                          : 'Measure again'}
                    </Button>
                    {measureError ? <ErrorState error={measureError} /> : null}
                  </div>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
