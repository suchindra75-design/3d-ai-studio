'use client';

import type { EnhanceRequest, ModelVersionRead, QualityMode } from '@ai3d/shared-types';
import { Button, Input, Label, Progress, Spinner } from '@ai3d/ui';
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
function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(Math.round(value), min), max);
}

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
  onView: (versionId: string, notice?: string, compareWith?: string) => void;
}) {
  const ids = {
    quality: useId(),
    axis: useId(),
    length: useId(),
    smoothing: useId(),
    edges: useId(),
    remesh: useId(),
    faces: useId(),
    resolution: useId(),
    upscale: useId(),
  };
  const [quality, setQuality] = useState<QualityMode>('high_quality');
  const [removeGround, setRemoveGround] = useState(version.source === 'reconstructed');
  const [useScale, setUseScale] = useState(false);
  const [axis, setAxis] = useState<Axis>('y');
  const [length, setLength] = useState('');
  const [smoothing, setSmoothing] = useState(0);
  const [smoothingMethod, setSmoothingMethod] = useState<'sharp' | 'taubin'>('sharp');
  const [remeshMethod, setRemeshMethod] = useState<'none' | 'quadriflow' | 'voxel'>('none');
  const [targetFaces, setTargetFaces] = useState('20000');
  const [resolution, setResolution] = useState('300');
  const [maps, setMaps] = useState<('normal' | 'occlusion')[]>([]);
  const [upscale, setUpscale] = useState<0 | 2 | 4>(0);
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
    smoothing,
    smoothing_method: smoothingMethod,
    upscale_textures: upscale === 0 ? null : upscale,
    remesh:
      remeshMethod === 'none'
        ? null
        : {
            method: remeshMethod,
            target_faces: clamp(Number(targetFaces) || 20_000, 100, 2_000_000),
            resolution: clamp(Number(resolution) || 300, 16, 2000),
            maps,
          },
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

      <fieldset className="flex flex-col gap-2 rounded-md border border-border p-3">
        <legend className="px-1 text-xs font-medium">Surface</legend>
        <div className="flex flex-col gap-1">
          <Label htmlFor={ids.smoothing} className="text-xs">
            Smoothing
          </Label>
          <select
            id={ids.smoothing}
            className={selectClass}
            value={smoothing}
            onChange={(event) => setSmoothing(Number(event.target.value))}
          >
            <option value={0}>Off</option>
            <option value={3}>Light (3 passes)</option>
            <option value={10}>Medium (10 passes)</option>
            <option value={30}>Strong (30 passes)</option>
          </select>
        </div>
        {smoothing ? (
          <div className="flex flex-col gap-1">
            <Label htmlFor={ids.edges} className="text-xs">
              Sharp edges
            </Label>
            <select
              id={ids.edges}
              className={selectClass}
              value={smoothingMethod}
              onChange={(event) => setSmoothingMethod(event.target.value as 'sharp' | 'taubin')}
            >
              <option value="sharp">Keep sharp edges and corners</option>
              <option value="taubin">Round them off (Taubin)</option>
            </select>
            <p className="text-[11px] leading-relaxed text-subtle-foreground">
              {smoothingMethod === 'sharp'
                ? 'Removes noise from flat and curved areas but leaves creases and corners where they are. Very rough surfaces (bumps as large as the triangles) smooth better when rounded off. Slower on very large models.'
                : 'Smooths everything evenly, edges included; suits organic shapes.'}{' '}
              Both keep the volume, texture coordinates and colours.
            </p>
          </div>
        ) : null}
        <div className="flex flex-col gap-1">
          <Label htmlFor={ids.upscale} className="text-xs">
            AI texture upscaling
          </Label>
          <select
            id={ids.upscale}
            className={selectClass}
            value={upscale}
            onChange={(event) => setUpscale(Number(event.target.value) as 0 | 2 | 4)}
          >
            <option value={0}>Off</option>
            <option value={2}>×2 (Real-ESRGAN, on this computer)</option>
            <option value={4}>×4 (Real-ESRGAN, on this computer)</option>
          </select>
          {upscale ? (
            <p className="text-[11px] leading-relaxed text-subtle-foreground">
              Makes textures look sharper by inventing plausible fine detail; it does not recover
              detail the photos never had. Up to the quality mode’s texture size; about a minute for
              a 2048 px texture.
            </p>
          ) : null}
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={ids.remesh} className="text-xs">
            Retopology
          </Label>
          <select
            id={ids.remesh}
            className={selectClass}
            value={remeshMethod}
            onChange={(event) => setRemeshMethod(event.target.value as typeof remeshMethod)}
          >
            <option value="none">Off</option>
            <option value="quadriflow">Even quad layout (QuadriFlow)</option>
            <option value="voxel">Uniform closed surface (voxels)</option>
          </select>
          {remeshMethod !== 'none' ? (
            <p className="text-[11px] leading-relaxed text-subtle-foreground">
              Rebuilt in Blender and kept only if it stays within 1% of the model’s size of the
              original. Colours are carried over; textures are baked onto new UVs. Each quad is
              saved as two triangles (GLB holds triangles).
            </p>
          ) : null}
        </div>
        {remeshMethod === 'quadriflow' ? (
          <div className="flex flex-col gap-1">
            <Label htmlFor={ids.faces} className="text-xs">
              About how many quads
            </Label>
            <Input
              id={ids.faces}
              type="number"
              inputMode="numeric"
              min={100}
              max={2_000_000}
              value={targetFaces}
              onChange={(event) => setTargetFaces(event.target.value)}
            />
          </div>
        ) : null}
        {remeshMethod === 'voxel' ? (
          <div className="flex flex-col gap-1">
            <Label htmlFor={ids.resolution} className="text-xs">
              Detail (voxels across the model)
            </Label>
            <Input
              id={ids.resolution}
              type="number"
              inputMode="numeric"
              min={16}
              max={2000}
              value={resolution}
              onChange={(event) => setResolution(event.target.value)}
            />
          </div>
        ) : null}
        {remeshMethod !== 'none' ? (
          <div className="flex flex-col gap-1.5 text-xs">
            {(
              [
                ['normal', 'Bake a normal map (keeps fine detail on fewer faces)'],
                ['occlusion', 'Bake ambient occlusion (soft shadows in creases)'],
              ] as const
            ).map(([value, label]) => (
              <label key={value} className="flex items-start gap-2 leading-relaxed">
                <input
                  type="checkbox"
                  className="mt-0.5 size-3.5 accent-accent"
                  checked={maps.includes(value)}
                  onChange={(event) =>
                    setMaps((current) =>
                      event.target.checked
                        ? [...current, value]
                        : current.filter((m) => m !== value),
                    )
                  }
                />
                {label}
              </label>
            ))}
          </div>
        ) : null}
      </fieldset>

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
              <Button
                size="sm"
                variant="ghost"
                onClick={() =>
                  onView(
                    result.result_model_version_id!,
                    `Enhanced from version ${version.version_number}, which is unchanged.`,
                    version.id,
                  )
                }
              >
                Compare before/after
              </Button>
            </div>
          ) : result.status === 'succeeded' ? (
            <span>
              {result.warnings.length
                ? 'No new version was created.'
                : 'Nothing needed changing, so no new version was created.'}
            </span>
          ) : result.status === 'failed' ? (
            <span className="text-danger">{result.error?.message ?? 'Enhancement failed.'}</span>
          ) : (
            <span>Cancelled.</span>
          )}
          {result.status === 'succeeded' && result.warnings.length ? (
            // What was asked for but not applied, with the reason (e.g. a retopology
            // that did not stay close enough to the original).
            <ul className="flex flex-col gap-1 text-warning" aria-label="Not applied">
              {result.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
