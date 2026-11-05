'use client';

/**
 * Re-texturing with a cloud service. The model is uploaded, so the person
 * confirms it here first (the backend asks again for local-only projects).
 */
import type { ModelVersionRead, QualityMode } from '@ai3d/shared-types';
import { Button, Label, Progress, Textarea } from '@ai3d/ui';
import { CloudUpload } from 'lucide-react';
import { useId, useState } from 'react';

import { useJob, useProviders, useRetexture } from '../api/hooks';
import { ErrorState } from '../components/page';
import { jobStageLabel, qualityModeLabel } from '../labels';

const selectClass =
  'h-9 rounded-md border border-border-strong bg-background px-2 text-sm outline-none focus:border-accent';

export function RetexturePanel({
  projectId,
  modelId,
  version,
  onView,
}: {
  projectId: string;
  modelId: string;
  version: ModelVersionRead;
  onView: (versionId: string, notice?: string) => void;
}) {
  const ids = { provider: useId(), prompt: useId(), quality: useId() };
  const providers = useProviders();
  const offered = (providers.data?.items ?? []).filter(
    (p) =>
      p.capabilities.retexture &&
      p.configured &&
      (!p.capabilities.retexture_own_models_only || p.key === version.provider_key),
  );
  const [chosen, setChosen] = useState<string | null>(null);
  const provider = offered.find((p) => p.key === chosen) ?? offered[0] ?? null;
  const [prompt, setPrompt] = useState('');
  const [quality, setQuality] = useState<QualityMode>('high_quality');
  const [consent, setConsent] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const retexture = useRetexture(projectId, modelId);
  const job = useJob(projectId, modelId, jobId);
  const result = job.data;
  const running =
    result && ['queued', 'running', 'cancelling', 'waiting_for_consent'].includes(result.status);

  if (providers.isPending) return null;
  const own = provider?.capabilities.retexture_own_models_only ?? false;
  const needsPrompt = !own;

  return (
    <section
      aria-labelledby="retexture-heading"
      className="flex flex-col gap-3 border-t border-border px-4 py-4"
    >
      <div>
        <h2 id="retexture-heading" className="text-xs font-medium">
          Re-texture with a cloud service
        </h2>
        <p className="mt-1 text-xs leading-relaxed text-subtle-foreground">
          An AI service paints new textures for version {version.version_number}. They are made up,
          not taken from the object, and the service may charge for it. The result is a new version.
        </p>
      </div>
      {offered.length === 0 ? (
        <p className="text-xs text-subtle-foreground">
          Add a Meshy API key in Settings to use this (Tripo can re-texture only models it
          generated).
        </p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor={ids.provider} className="text-xs">
                Service
              </Label>
              <select
                id={ids.provider}
                className={selectClass}
                value={provider?.key ?? ''}
                onChange={(event) => {
                  setChosen(event.target.value);
                  setConsent(false);
                }}
              >
                {offered.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.display_name}
                  </option>
                ))}
              </select>
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
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor={ids.prompt} className="text-xs">
              Describe the texture{own ? ' (optional)' : ''}
            </Label>
            <Textarea
              id={ids.prompt}
              rows={2}
              maxLength={800}
              placeholder="e.g. weathered bronze with green patina"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
            />
          </div>
          <label className="flex items-start gap-2 text-xs leading-relaxed">
            <input
              type="checkbox"
              className="mt-0.5 size-3.5 accent-accent"
              checked={consent}
              onChange={(event) => setConsent(event.target.checked)}
            />
            <span>
              {own
                ? `Ask ${provider?.display_name} to re-texture the model it made (nothing new is uploaded).`
                : `Upload version ${version.version_number} of the model to ${provider?.display_name}.`}
            </span>
          </label>
          {retexture.isError ? <ErrorState error={retexture.error} /> : null}
          <Button
            variant="secondary"
            disabled={
              !provider ||
              !consent ||
              (needsPrompt && !prompt.trim()) ||
              retexture.isPending ||
              Boolean(running)
            }
            onClick={() =>
              provider &&
              retexture.mutate(
                {
                  provider: provider.key,
                  prompt,
                  quality_mode: quality,
                  source_version_id: version.id,
                  cloud_upload_consent: consent,
                },
                { onSuccess: (created) => setJobId(created.id) },
              )
            }
          >
            <CloudUpload aria-hidden /> Re-texture with {provider?.display_name}
          </Button>
        </>
      )}
      {result ? (
        <div role="status" aria-live="polite" className="flex flex-col gap-2 text-xs">
          {result.status === 'waiting_for_consent' ? (
            <span className="text-warning">
              Waiting for permission: this project keeps its data on this computer. Allow it on the
              project page’s job list to continue; nothing has been sent.
            </span>
          ) : running ? (
            <>
              <span>
                {jobStageLabel[result.stage ?? ''] ?? 'Queued'} ·{' '}
                {Math.round(result.progress * 100)}%
              </span>
              <Progress value={result.progress * 100} aria-label="Re-texturing progress" />
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
                    `Re-textured from version ${version.version_number}, which is unchanged.`,
                  )
                }
              >
                View the re-textured version
              </Button>
            </div>
          ) : result.status === 'failed' ? (
            <span className="text-danger">{result.error?.message ?? 'Re-texturing failed.'}</span>
          ) : (
            <span>Cancelled.</span>
          )}
        </div>
      ) : null}
    </section>
  );
}
