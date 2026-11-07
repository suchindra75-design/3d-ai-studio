'use client';

import type { JobRead, ProviderSummary } from '@ai3d/shared-types';
import { Badge, Button, ConfirmDialog, Progress } from '@ai3d/ui';
import { formatRelativeTime, pluralize } from '@ai3d/utilities';
import { ChevronDown, ChevronRight, Cloud, Cpu, RotateCw, X } from 'lucide-react';
import { useRef, useState } from 'react';

import {
  isJobActive,
  useCancelJob,
  useConsentJob,
  useJobEvents,
  useJobStream,
  useRetryJob,
} from '../api/hooks';
import { formatEstimate, newIdempotencyKey } from '../generation';
import { jobStageLabel, jobStatusLabel, jobStatusTone, qualityModeLabel } from '../labels';
import { ErrorState } from './page';

function Cost({ job }: { job: JobRead }) {
  if (job.cost && job.cost.credits !== null) {
    const usd = job.cost.usd ? ` (US$${job.cost.usd.toFixed(2)})` : '';
    return (
      <span>
        Charged by provider: {job.cost.credits.toLocaleString()} credits{usd}
      </span>
    );
  }
  if (job.estimate && !['succeeded', 'failed', 'cancelled'].includes(job.status)) {
    return <span>Estimated: {formatEstimate(job.estimate)}</span>;
  }
  return null;
}

function Timeline({ jobId }: { jobId: string }) {
  const events = useJobEvents(jobId, true);
  if (events.isError) return <ErrorState error={events.error} />;
  if (!events.data) return null;
  return (
    <ol className="flex flex-col gap-1 border-l border-border pl-3 text-xs">
      {events.data.items.map((event) => (
        <li
          key={event.seq}
          className={
            event.type === 'error'
              ? 'text-danger'
              : event.type === 'warning'
                ? 'text-warning'
                : 'text-muted-foreground'
          }
        >
          <time className="mr-2 text-subtle-foreground" dateTime={event.created_at}>
            {new Date(event.created_at).toLocaleTimeString()}
          </time>
          {event.message}
        </li>
      ))}
    </ol>
  );
}

function ConsentPrompt({ job, projectId }: { job: JobRead; projectId: string }) {
  const consent = useConsentJob(projectId);
  const cancel = useCancelJob(projectId);
  const [allowProject, setAllowProject] = useState(false);
  const localOnly = job.consent_reason === 'project_local_only';
  const name = job.provider?.display_name ?? 'the provider';
  return (
    <div className="flex flex-col gap-2.5 rounded-md bg-warning-soft p-3 text-xs leading-relaxed">
      <p>
        This job will send{' '}
        {job.kind === 'retexture' ? 'the model' : pluralize(job.inputs.length, 'photo')} to {name}.
        Nothing has been uploaded yet.
      </p>
      {localOnly ? (
        <label className="flex gap-2">
          <input
            type="checkbox"
            className="mt-0.5 size-4 shrink-0 accent-accent"
            checked={allowProject}
            onChange={(event) => setAllowProject(event.target.checked)}
          />
          This project is set to keep photos local. Allow it to use cloud providers.
        </label>
      ) : null}
      {consent.isError ? <ErrorState error={consent.error} /> : null}
      <div className="flex gap-2">
        <Button
          size="sm"
          variant="primary"
          disabled={(localOnly && !allowProject) || consent.isPending}
          onClick={() => consent.mutate({ jobId: job.id, allowProject })}
        >
          Allow and start
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={cancel.isPending}
          onClick={() => cancel.mutate(job.id)}
        >
          Don’t send
        </Button>
      </div>
    </div>
  );
}

function JobCard({ job, projectId }: { job: JobRead; projectId: string }) {
  const active = isJobActive(job);
  useJobStream(projectId, job.id, active);
  const cancel = useCancelJob(projectId);
  const retry = useRetryJob(projectId);
  const [expanded, setExpanded] = useState(false);
  const [confirmRetry, setConfirmRetry] = useState(false);
  // Another engine the failed job offered, waiting for the person to confirm.
  const [switchTo, setSwitchTo] = useState<ProviderSummary | null>(null);
  const [confirmSwitch, setConfirmSwitch] = useState(false);
  const retryKey = useRef(newIdempotencyKey());
  // Repairs (D70) run as enhance jobs that do only the operations asked for.
  const isRepair = job.kind === 'enhance' && job.provider_summary[0]?.startsWith('Repair:');
  const name = isRepair
    ? 'Repair (on this computer)'
    : job.kind === 'enhance'
      ? 'Enhance (on this computer)'
      : (job.provider?.display_name ?? 'Unknown provider');
  const uploads = job.provider?.uploads_images ?? false;
  const photos = pluralize(job.inputs.length, 'photo');

  function runRetry(provider?: ProviderSummary) {
    retry.mutate(
      {
        jobId: job.id,
        // Confirming the dialog is the consent: it names the service and what is sent.
        consent: provider ? provider.uploads_images : uploads,
        idempotencyKey: retryKey.current,
        provider: provider?.key,
      },
      { onSuccess: () => (retryKey.current = newIdempotencyKey()) },
    );
  }

  return (
    <li className="flex flex-col gap-2.5 rounded-lg border border-border bg-surface p-3.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-[13px] font-medium">
            {isRepair ? name : `${name} · ${qualityModeLabel[job.quality_mode]}`}
          </span>
          <span className="text-xs text-subtle-foreground">
            {job.kind === 'enhance' || job.kind === 'retexture'
              ? 'Model version'
              : pluralize(job.inputs.length, 'photo')}{' '}
            · {formatRelativeTime(job.created_at)}
          </span>
        </div>
        <Badge variant={jobStatusTone[job.status]}>{jobStatusLabel[job.status]}</Badge>
      </div>

      {active ? (
        <div className="flex flex-col gap-1.5">
          <Progress value={job.progress * 100} aria-label="Generation progress" />
          <span className="text-xs text-muted-foreground">
            {job.status === 'queued'
              ? 'Waiting for a free worker'
              : job.status === 'cancelling'
                ? 'Cancelling…'
                : (jobStageLabel[job.stage ?? ''] ?? 'Working')}{' '}
            · {Math.round(job.progress * 100)}%
          </span>
        </div>
      ) : null}

      {job.status === 'waiting_for_consent' ? (
        <ConsentPrompt job={job} projectId={projectId} />
      ) : null}

      {job.error ? (
        <p role="alert" className="text-xs leading-relaxed text-danger">
          {job.error.message}
        </p>
      ) : null}

      {job.status === 'failed' && job.alternatives.length > 0 ? (
        <div className="flex flex-col gap-2 rounded-md bg-surface-raised p-3 text-xs leading-relaxed">
          <p className="font-medium">Try another engine</p>
          <ul className="flex flex-col gap-2">
            {job.alternatives.map((alternative) => (
              <li
                key={alternative.provider.key}
                className="flex flex-wrap items-center justify-between gap-2"
              >
                <span className="text-muted-foreground">{alternative.reason}</span>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={retry.isPending}
                  onClick={() => {
                    setSwitchTo(alternative.provider);
                    setConfirmSwitch(true);
                  }}
                >
                  {alternative.provider.uploads_images ? (
                    <Cloud aria-hidden />
                  ) : (
                    <Cpu aria-hidden />
                  )}{' '}
                  Use {alternative.provider.display_name}
                </Button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-subtle-foreground">
        <Cost job={job} />
        <div className="flex gap-1.5">
          {job.can_cancel && job.status !== 'waiting_for_consent' ? (
            <Button
              size="sm"
              variant="ghost"
              disabled={cancel.isPending}
              onClick={() => cancel.mutate(job.id)}
            >
              <X aria-hidden /> Cancel
            </Button>
          ) : null}
          {job.can_retry ? (
            <Button size="sm" variant="secondary" onClick={() => setConfirmRetry(true)}>
              <RotateCw aria-hidden /> Retry
            </Button>
          ) : null}
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={expanded}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? <ChevronDown aria-hidden /> : <ChevronRight aria-hidden />} Details
          </Button>
        </div>
      </div>
      {retry.isError ? <ErrorState error={retry.error} /> : null}
      {expanded ? (
        <div className="flex flex-col gap-2">
          {job.provider_settings && Object.keys(job.provider_settings).length > 0 ? (
            <p className="text-xs text-muted-foreground">{job.provider_summary.join(' · ')}</p>
          ) : null}
          {job.provider_task_id ? (
            <p className="text-xs text-subtle-foreground">
              {name} task: <code className="font-mono">{job.provider_task_id}</code>
            </p>
          ) : null}
          <Timeline jobId={job.id} />
        </div>
      ) : null}
      <ConfirmDialog
        open={confirmRetry}
        onOpenChange={setConfirmRetry}
        title="Run this generation again?"
        description={
          uploads
            ? `This sends ${photos} to ${name} again as a new job, which ${name} may charge for.`
            : 'This starts a new job with the same photos and settings.'
        }
        confirmLabel="Run again"
        onConfirm={() => runRetry()}
      />
      <ConfirmDialog
        open={confirmSwitch}
        onOpenChange={setConfirmSwitch}
        title={
          switchTo?.uploads_images
            ? `Send ${photos} to ${switchTo.display_name}?`
            : `Run on this computer with ${switchTo?.display_name ?? ''}?`
        }
        description={
          switchTo?.uploads_images
            ? `${switchTo.display_name} is a cloud service: this uploads ${photos} to it as a new job, which it may charge for. Nothing is sent until you confirm.`
            : 'This starts a new job with the same photos on this computer. Nothing is uploaded or charged.'
        }
        confirmLabel={switchTo?.uploads_images ? 'Send and start' : 'Start'}
        onConfirm={() => (switchTo ? runRetry(switchTo) : undefined)}
      />
    </li>
  );
}

export function JobList({ jobs, projectId }: { jobs: JobRead[]; projectId: string }) {
  if (jobs.length === 0) return null;
  return (
    <section aria-labelledby="jobs-heading" className="flex flex-col gap-3">
      <h2 id="jobs-heading" className="text-sm font-medium">
        Generations
      </h2>
      <ul className="flex flex-col gap-2">
        {jobs.map((job) => (
          <JobCard key={job.id} job={job} projectId={projectId} />
        ))}
      </ul>
    </section>
  );
}
