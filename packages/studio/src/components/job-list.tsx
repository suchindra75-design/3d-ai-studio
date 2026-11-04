'use client';

import type { JobRead } from '@ai3d/shared-types';
import { Badge, Button, ConfirmDialog, Progress } from '@ai3d/ui';
import { formatRelativeTime, pluralize } from '@ai3d/utilities';
import { ChevronDown, ChevronRight, RotateCw, X } from 'lucide-react';
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
        This job will send {pluralize(job.inputs.length, 'photo')} to {name}. Nothing has been
        uploaded yet.
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
  const retryKey = useRef(newIdempotencyKey());
  const name =
    job.kind === 'enhance'
      ? 'Enhance (on this computer)'
      : (job.provider?.display_name ?? 'Unknown provider');
  const uploads = job.provider?.uploads_images ?? false;

  function runRetry() {
    retry.mutate(
      { jobId: job.id, consent: uploads, idempotencyKey: retryKey.current },
      { onSuccess: () => (retryKey.current = newIdempotencyKey()) },
    );
  }

  return (
    <li className="flex flex-col gap-2.5 rounded-lg border border-border bg-surface p-3.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-[13px] font-medium">
            {name} · {qualityModeLabel[job.quality_mode]}
          </span>
          <span className="text-xs text-subtle-foreground">
            {job.kind === 'enhance' ? 'Model version' : pluralize(job.inputs.length, 'photo')} ·{' '}
            {formatRelativeTime(job.created_at)}
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
            ? `This sends ${pluralize(job.inputs.length, 'photo')} to ${name} again as a new job, which ${name} may charge for.`
            : 'This starts a new job with the same photos and settings.'
        }
        confirmLabel="Run again"
        onConfirm={runRetry}
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
