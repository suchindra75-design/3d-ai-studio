'use client';

import type {
  GenerationCreate,
  GenerationPlanRead,
  ImageRead,
  ProcessingMode,
  ProjectRead,
  QualityMode,
} from '@ai3d/shared-types';
import { Badge, Button, Card, CardContent, CardHeader, CardTitle, Label, Spinner } from '@ai3d/ui';
import { pluralize } from '@ai3d/utilities';
import { AlertTriangle, Box, CloudUpload, Info } from 'lucide-react';
import { useId, useRef, useState } from 'react';

import {
  useCreateGeneration,
  useGenerationPlan,
  useProviders,
  useUpdateProject,
} from '../api/hooks';
import { useStudio } from '../context';
import { formatEstimate, newIdempotencyKey } from '../generation';
import { processingModeLabel, qualityModeLabel, viewRoleLabel } from '../labels';
import { routes } from '../routes';
import { ErrorState } from './page';

type Role = 'source' | 'front' | 'left' | 'back' | 'right';
const MULTI_ROLES: Role[] = ['front', 'left', 'back', 'right'];
type Method = 'photogrammetry' | 'ai';
const PHOTOGRAMMETRY = 'local-photogrammetry';

const selectClass =
  'h-9 w-full rounded-md border border-border-strong bg-surface px-2.5 text-[13px] text-foreground focus-visible:outline-2 focus-visible:outline-accent';

function PlanSummary({ plan }: { plan: GenerationPlanRead }) {
  const { Link } = useStudio().navigation;
  const needsSettings = plan.blocking_issues.some((issue) =>
    ['provider_not_configured', 'no_provider'].includes(issue.code),
  );
  return (
    <div className="flex flex-col gap-3 text-[13px]">
      {plan.provider ? (
        <div className="flex flex-col gap-1.5">
          <p className="flex flex-wrap items-center gap-1.5">
            <span className="font-medium">{plan.provider.display_name}</span>
            <Badge variant={plan.route === 'cloud' ? 'accent' : 'neutral'}>
              {plan.route === 'cloud' ? 'Cloud' : 'Local'}
            </Badge>
          </p>
          {plan.provider_summary.length > 0 ? (
            <p className="text-xs text-muted-foreground">{plan.provider_summary.join(' · ')}</p>
          ) : null}
          {plan.estimate ? (
            <p className="text-xs">
              <span className="font-medium">Estimated cost: {formatEstimate(plan.estimate)}</span>
              <span className="block text-subtle-foreground">{plan.estimate.basis}</span>
            </p>
          ) : null}
        </div>
      ) : null}

      {plan.blocking_issues.length > 0 ? (
        <ul role="alert" className="flex flex-col gap-1.5 rounded-md bg-danger-soft p-2.5 text-xs">
          {plan.blocking_issues.map((issue) => (
            <li key={issue.code} className="text-danger">
              {issue.message}
            </li>
          ))}
          {needsSettings ? (
            <li>
              <Link href={routes.settings} className="font-medium text-foreground underline">
                Open Settings → AI providers
              </Link>
            </li>
          ) : null}
        </ul>
      ) : null}

      {plan.warnings.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {plan.warnings.map((warning) => (
            <li key={warning} className="flex gap-2 text-xs leading-relaxed text-muted-foreground">
              <Info className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
              {warning}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function GenerationPanel({
  project,
  images,
}: {
  project: ProjectRead;
  images: ImageRead[];
}) {
  const ids = {
    quality: useId(),
    processing: useId(),
    provider: useId(),
    consent: useId(),
    method: useId(),
  };
  const providers = useProviders();
  const create = useCreateGeneration(project.id);
  const updateProject = useUpdateProject(project.id);
  const multi = project.input_mode === 'multi_view';

  const [slots, setSlots] = useState<Partial<Record<Role, string>>>({});
  const [quality, setQuality] = useState<QualityMode>(project.default_quality_mode);
  const [processing, setProcessing] = useState<ProcessingMode>(project.default_processing_mode);
  const [provider, setProvider] = useState('');
  const [consent, setConsent] = useState(false);
  const [allowProject, setAllowProject] = useState(false);
  const [chosenMethod, setMethod] = useState<Method | null>(null);
  const [excluded, setExcluded] = useState<ReadonlySet<string>>(new Set());
  const idempotencyKey = useRef(newIdempotencyKey());
  // Many photos are a capture set for photogrammetry; AI multi-view takes at most four.
  const method: Method = multi
    ? (chosenMethod ?? (images.length > MULTI_ROLES.length ? 'photogrammetry' : 'ai'))
    : 'ai';
  const photogrammetry = method === 'photogrammetry';

  const imageIds = new Set(images.map((image) => image.id));
  const roles: Role[] = multi ? MULTI_ROLES : ['source'];
  const firstRole = roles[0]!;
  // The first slot defaults to the first photo; a removed photo clears its slot.
  const chosen = roles.map((role): [Role, string] => {
    const value = slots[role];
    if (value && imageIds.has(value)) return [role, value];
    return [role, role === firstRole ? (images[0]?.id ?? '') : ''];
  });
  const inputs = chosen.filter(([, id]) => id);
  const duplicate = new Set(inputs.map(([, id]) => id)).size !== inputs.length;
  const capture = images.filter((image) => !excluded.has(image.id));

  // Query keys are compared structurally, so a fresh object per render is fine.
  const body: GenerationCreate | null = photogrammetry
    ? capture.length === 0
      ? null
      : {
          inputs: capture.map((image) => ({ image_id: image.id, role: 'source' })),
          provider: PHOTOGRAMMETRY,
          quality_mode: quality,
          processing_mode: 'local',
        }
    : inputs.length === 0 || duplicate
      ? null
      : {
          inputs: inputs.map(([role, image_id]) => ({ image_id, role })),
          provider: provider || null,
          quality_mode: quality,
          processing_mode: processing,
        };

  const plan = useGenerationPlan(project.id, body);
  const planData = plan.data;
  const needsProjectPermission = planData?.consent_reason === 'project_local_only';
  const ready =
    body !== null &&
    planData?.ok === true &&
    !plan.isFetching &&
    (!planData.requires_consent || (consent && (!needsProjectPermission || allowProject)));

  async function generate() {
    if (!body || !planData) return;
    if (planData.requires_consent && needsProjectPermission) {
      await updateProject.mutateAsync({ allow_cloud_upload: true });
    }
    create.mutate(
      {
        body: { ...body, cloud_upload_consent: planData.requires_consent && consent },
        idempotencyKey: idempotencyKey.current,
      },
      {
        onSuccess: () => {
          idempotencyKey.current = newIdempotencyKey();
          setConsent(false);
          setAllowProject(false);
        },
      },
    );
  }

  // Local engines are chosen through the method, not the provider list.
  const configured = (providers.data?.items ?? []).filter((p) => p.uploads_images !== false);

  function toggleExcluded(id: string) {
    const next = new Set(excluded);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setExcluded(next);
  }
  const photoOptions = images.map((image) => (
    <option key={image.id} value={image.id}>
      {image.original_filename}
    </option>
  ));

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Box className="size-4 text-accent" aria-hidden />{' '}
          {photogrammetry ? 'Reconstruct 3D model' : 'Generate 3D model'}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {images.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">Upload a photo to generate a model.</p>
        ) : (
          <>
            {multi ? (
              <div className="flex flex-col gap-1.5">
                <span className="text-xs font-medium text-muted-foreground" id={ids.method}>
                  Method
                </span>
                <div
                  role="radiogroup"
                  aria-labelledby={ids.method}
                  className="grid grid-cols-2 gap-1 rounded-md border border-border-strong p-0.5"
                >
                  {(
                    [
                      ['photogrammetry', 'Photogrammetry', 'Measured, on this computer'],
                      ['ai', 'AI multi-view', '2–4 views, cloud'],
                    ] as const
                  ).map(([value, label, hint]) => (
                    <button
                      key={value}
                      type="button"
                      role="radio"
                      aria-checked={method === value}
                      className={
                        method === value
                          ? 'rounded bg-surface-hover px-2 py-1.5 text-left'
                          : 'rounded px-2 py-1.5 text-left text-muted-foreground hover:text-foreground'
                      }
                      onClick={() => setMethod(value)}
                    >
                      <span className="block text-xs font-medium">{label}</span>
                      <span className="block text-[11px] text-subtle-foreground">{hint}</span>
                    </button>
                  ))}
                </div>
              </div>
            ) : null}

            {photogrammetry ? (
              <fieldset className="flex flex-col gap-1.5">
                <legend className="mb-1 text-xs font-medium text-muted-foreground">Photos</legend>
                <p className="text-xs">
                  Uses {capture.length} of {pluralize(images.length, 'photo')}. COLMAP finds where
                  each was taken; no view labels needed.
                </p>
                <details className="text-xs">
                  <summary className="cursor-pointer text-accent">Choose photos</summary>
                  <ul className="mt-2 flex max-h-48 flex-col gap-1 overflow-y-auto">
                    {images.map((image) => (
                      <li key={image.id}>
                        <label className="flex items-center gap-2">
                          <input
                            type="checkbox"
                            className="size-3.5 accent-accent"
                            checked={!excluded.has(image.id)}
                            onChange={() => toggleExcluded(image.id)}
                          />
                          <span className="truncate">{image.original_filename}</span>
                        </label>
                      </li>
                    ))}
                  </ul>
                </details>
              </fieldset>
            ) : (
              <fieldset className="flex flex-col gap-2">
                <legend className="mb-1 text-xs font-medium text-muted-foreground">
                  {multi ? 'Views (front required, up to 4)' : 'Photo'}
                </legend>
                <div className={multi ? 'grid grid-cols-2 gap-2' : 'grid gap-2'}>
                  {chosen.map(([role, value]) => (
                    <label key={role} className="flex flex-col gap-1 text-xs">
                      {multi ? (
                        <span className="text-subtle-foreground">{viewRoleLabel[role]}</span>
                      ) : null}
                      <select
                        className={selectClass}
                        value={value}
                        aria-label={`${viewRoleLabel[role]} photo`}
                        onChange={(event) => setSlots({ ...slots, [role]: event.target.value })}
                      >
                        {role !== firstRole ? <option value="">None</option> : null}
                        {photoOptions}
                      </select>
                    </label>
                  ))}
                </div>
                {duplicate ? (
                  <p className="text-xs text-danger">Use each photo for one view only.</p>
                ) : null}
              </fieldset>
            )}

            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1">
                <Label htmlFor={ids.quality}>Quality</Label>
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
              {photogrammetry ? (
                <div className="flex flex-col gap-1">
                  <span className="text-sm font-medium">Processing</span>
                  <span className="flex h-9 items-center text-[13px] text-muted-foreground">
                    On this computer
                  </span>
                </div>
              ) : (
                <div className="flex flex-col gap-1">
                  <Label htmlFor={ids.processing}>Processing</Label>
                  <select
                    id={ids.processing}
                    className={selectClass}
                    value={processing}
                    onChange={(event) => setProcessing(event.target.value as ProcessingMode)}
                  >
                    {(Object.keys(processingModeLabel) as ProcessingMode[]).map((mode) => (
                      <option key={mode} value={mode}>
                        {processingModeLabel[mode]}
                      </option>
                    ))}
                  </select>
                </div>
              )}
            </div>
            {photogrammetry ? null : (
              <div className="flex flex-col gap-1">
                <Label htmlFor={ids.provider}>Provider</Label>
                <select
                  id={ids.provider}
                  className={selectClass}
                  value={provider}
                  onChange={(event) => setProvider(event.target.value)}
                >
                  <option value="">Choose automatically</option>
                  {configured.map((p) => (
                    <option key={p.key} value={p.key} disabled={!p.configured}>
                      {p.display_name}
                      {p.configured ? '' : ' (no API key)'}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {plan.isError ? (
              <ErrorState error={plan.error} onRetry={() => void plan.refetch()} />
            ) : planData ? (
              <PlanSummary plan={planData} />
            ) : body ? (
              <Spinner label="Checking options" />
            ) : null}

            {planData?.ok && planData.requires_consent && planData.provider ? (
              <div className="flex flex-col gap-2.5 rounded-md border border-border-strong p-3">
                <label className="flex gap-2.5 text-xs leading-relaxed">
                  <input
                    type="checkbox"
                    className="mt-0.5 size-4 shrink-0 accent-accent"
                    checked={consent}
                    onChange={(event) => setConsent(event.target.checked)}
                  />
                  <span>
                    <CloudUpload className="mr-1 inline size-3.5 text-accent" aria-hidden />
                    Send {pluralize(planData.inputs.length, 'photo')} to{' '}
                    {planData.provider.display_name} for this generation. Copies are resized and
                    stripped of location and camera data; your originals stay on this machine.
                    {planData.estimate && planData.estimate.amount > 0
                      ? ` ${planData.provider.display_name} bills your account.`
                      : ''}
                  </span>
                </label>
                {needsProjectPermission ? (
                  <label className="flex gap-2.5 text-xs leading-relaxed">
                    <input
                      type="checkbox"
                      className="mt-0.5 size-4 shrink-0 accent-accent"
                      checked={allowProject}
                      onChange={(event) => setAllowProject(event.target.checked)}
                    />
                    <span>
                      <AlertTriangle className="mr-1 inline size-3.5 text-warning" aria-hidden />
                      This project is set to keep photos local. Allow it to use cloud providers.
                    </span>
                  </label>
                ) : null}
              </div>
            ) : null}

            {create.isError ? <ErrorState error={create.error} /> : null}
            {updateProject.isError ? <ErrorState error={updateProject.error} /> : null}
            <Button
              variant="primary"
              disabled={!ready || create.isPending || updateProject.isPending}
              onClick={() => void generate()}
            >
              {create.isPending ? 'Starting…' : photogrammetry ? 'Reconstruct' : 'Generate'}
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}
