'use client';

import type { EnhanceRequest, ModelVersionRead, QualityMode } from '@ai3d/shared-types';
import { Button, Label, Progress, Spinner } from '@ai3d/ui';
import { CircleCheck, CircleX } from 'lucide-react';
import { useId, useRef, useState } from 'react';

import { useEnhance, useEnhancePlan, useJob } from '../api/hooks';
import { ErrorState } from '../components/page';
import { newIdempotencyKey } from '../generation';
import { jobStageLabel, qualityModeLabel } from '../labels';

const selectClass =
  'h-8 w-full rounded-md border border-border-strong bg-surface px-2 text-xs text-foreground';

type Axis = 'x' | 'y' | 'z' | 'longest';
const AXES: { value: Axis; label: string }[] = [
  { value: 'y', label: 'Height (Y)' },
  { value: 'x', label: 'Width (X)' },
  { value: 'z', label: 'Depth (Z)' },
  { value: 'longest', label: 'Longest side' },
];

/** Process the viewed version into a new version (the source is never changed). */
export function EnhancePanel({
  projectId,
  modelId,
  version,
  dirty,
  onView,
}: {
  projectId: string;
  modelId: string;
  version: ModelVersionRead;
  dirty: boolean;
  onView: (versionId: string, notice?: string) => void;
}) {
  const ids = { quality: useId(), axis: useId(), length: useId() };
  const [quality, setQuality] = useState<QualityMode>('high_quality');
  const [removeGround, setRemoveGround] = useState(version.source === 'reconstructed');
  const [useScale, setUseScale] = useState(false);
  const [axis, setAxis] = useState<Axis>('y');
  const [length, setLength] = useState('');
  const [jobId, setJobId] = useState<string | null>(null);
  const key = useRef(newIdempotencyKey());

  const lengthMm = Number(length);
  const scaleValid =
    !useScale || (Number.isFinite(lengthMm) && lengthMm > 0 && lengthMm <= 100_000);
  const body: EnhanceRequest = {
    quality_mode: quality,
    source_version_id: version.id,
    remove_ground: removeGround,
    scale: useScale && scaleValid && length ? { axis, length_mm: lengthMm } : null,
  };
  const plan = useEnhancePlan(modelId, body);
  const enhance = useEnhance(projectId, modelId);
  const job = useJob(projectId, modelId, jobId);
  const running = job.data && ['queued', 'running', 'cancelling'].includes(job.data.status);

  function start() {
    enhance.mutate(
      { body, idempotencyKey: key.current },
      {
        onSuccess: (created) => {
          key.current = newIdempotencyKey();
          setJobId(created.id);
        },
      },
    );
  }

  const result = job.data;
  return (
    <section aria-labelledby="enhance-heading" className="flex flex-col gap-4 px-4 py-4">
      <div>
        <h2 id="enhance-heading" className="text-xs font-medium">
          Enhance version {version.version_number}
        </h2>
        <p className="mt-1 text-xs leading-relaxed text-subtle-foreground">
          Cleans and optimises the geometry and textures on this computer. The result is saved as a
          new version; version {version.version_number} is kept unchanged.
        </p>
        {dirty ? (
          <p className="mt-1 text-xs text-warning">
            Unsaved edits are not included. Save them as a version first to enhance them.
          </p>
        ) : null}
      </div>

      <div className="flex flex-col gap-1">
        <Label htmlFor={ids.quality} className="text-xs">
          Quality
        </Label>
        <select
          id={ids.quality}
          className={selectClass}
          value={quality}
          onChange={(event) => setQuality(event.target.value as QualityMode)}
        >
          {(Object.keys(qualityModeLabel) as QualityMode[]).map((mode) => (
            <option key={mode} value={mode}>
              {qualityModeLabel[mode]}
            </option>
          ))}
        </select>
      </div>

      <label className="flex items-start gap-2 text-xs leading-relaxed">
        <input
          type="checkbox"
          className="mt-0.5 size-3.5 accent-accent"
          checked={removeGround}
          onChange={(event) => setRemoveGround(event.target.checked)}
        />
        <span>
          Remove the ground or table under the object
          <span className="block text-subtle-foreground">For photogrammetry scans.</span>
        </span>
      </label>

      <fieldset className="flex flex-col gap-2">
        <label className="flex items-start gap-2 text-xs leading-relaxed">
          <input
            type="checkbox"
            className="mt-0.5 size-3.5 accent-accent"
            checked={useScale}
            onChange={(event) => setUseScale(event.target.checked)}
          />
          <span>
            Set the real-world size from a measurement
            <span className="block text-subtle-foreground">
              Measure one side of the real object; the model is scaled to match.
            </span>
          </span>
        </label>
        {useScale ? (
          <div className="grid grid-cols-2 gap-2 pl-5">
            <div className="flex flex-col gap-1">
              <Label htmlFor={ids.axis} className="text-[11px]">
                Measured side
              </Label>
              <select
                id={ids.axis}
                className={selectClass}
                value={axis}
                onChange={(event) => setAxis(event.target.value as Axis)}
              >
                {AXES.map((a) => (
                  <option key={a.value} value={a.value}>
                    {a.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor={ids.length} className="text-[11px]">
                Length (mm)
              </Label>
              <input
                id={ids.length}
                type="number"
                inputMode="decimal"
                min={0}
                step="any"
                className={selectClass}
                value={length}
                aria-invalid={!scaleValid || undefined}
                onChange={(event) => setLength(event.target.value)}
              />
            </div>
          </div>
        ) : null}
      </fieldset>

      {plan.data ? (
        <div className="flex flex-col gap-1.5">
          <p className="text-xs font-medium">Steps</p>
          <ul className="flex flex-col gap-1 text-xs" aria-label="Enhancement steps">
            {plan.data.steps.map((step) => (
              <li key={step.code} className="flex gap-2">
                {step.status === 'will_run' ? (
                  <CircleCheck className="mt-0.5 size-3.5 shrink-0 text-success" aria-hidden />
                ) : (
                  <CircleX
                    className="mt-0.5 size-3.5 shrink-0 text-subtle-foreground"
                    aria-hidden
                  />
                )}
                <span className={step.status === 'will_run' ? '' : 'text-subtle-foreground'}>
                  <span className="font-medium">{step.title}</span>
                  {step.status === 'unavailable' ? ' (unavailable)' : ''} — {step.detail}
                </span>
              </li>
            ))}
          </ul>
          {plan.data.notes.map((note) => (
            <p key={note} className="text-[11px] leading-relaxed text-subtle-foreground">
              {note}
            </p>
          ))}
        </div>
      ) : plan.isError ? (
        <ErrorState error={plan.error} />
      ) : (
        <Spinner label="Loading steps" />
      )}

      {enhance.isError ? <ErrorState error={enhance.error} /> : null}
      <Button
        variant="primary"
        disabled={!scaleValid || (useScale && !length) || enhance.isPending || Boolean(running)}
        onClick={start}
      >
        {enhance.isPending || running ? 'Enhancing…' : `Enhance (${qualityModeLabel[quality]})`}
      </Button>

      {result ? (
        <div role="status" aria-live="polite" className="flex flex-col gap-2 text-xs">
          {running ? (
            <>
              <span>
                {jobStageLabel[result.stage ?? ''] ?? 'Queued'} ·{' '}
                {Math.round(result.progress * 100)}%
              </span>
              <Progress value={result.progress * 100} aria-label="Enhancement progress" />
            </>
          ) : result.status === 'succeeded' && result.result_model_version_id ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-success">Done: saved as a new version.</span>
              <Button
                size="sm"
                variant="secondary"
                onClick={() =>
                  onView(
                    result.result_model_version_id!,
                    `Enhanced from version ${version.version_number}, which is unchanged.`,
                  )
                }
              >
                View the new version
              </Button>
            </div>
          ) : result.status === 'succeeded' ? (
            <span>Nothing needed changing, so no new version was created.</span>
          ) : result.status === 'failed' ? (
            <span className="text-danger">{result.error?.message ?? 'Enhancement failed.'}</span>
          ) : (
            <span>Cancelled.</span>
          )}
        </div>
      ) : null}
    </section>
  );
}
