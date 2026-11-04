'use client';

import { Button, cn } from '@ai3d/ui';
import { AlertTriangle, RotateCw } from 'lucide-react';
import type { ReactNode } from 'react';
import { ApiError } from '@ai3d/api-client';

export function PageContainer({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn('mx-auto w-full max-w-6xl px-4 py-6 md:px-8 md:py-8', className)}>
      {children}
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        {eyebrow ? <div className="mb-2">{eyebrow}</div> : null}
        <h1 className="truncate text-2xl font-semibold tracking-tight">{title}</h1>
        {description ? <p className="mt-1 text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode;
  title: string;
  description: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center rounded-lg border border-dashed border-border-strong px-6 py-14 text-center">
      <div className="mb-4 flex size-11 items-center justify-center rounded-full bg-surface-raised text-muted-foreground [&_svg]:size-5">
        {icon}
      </div>
      <h2 className="text-base font-semibold">{title}</h2>
      <p className="mt-1 max-w-sm text-sm text-muted-foreground">{description}</p>
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const offline = error instanceof ApiError && error.isNetworkError;
  const notFound = error instanceof ApiError && error.status === 404;
  const message =
    error instanceof Error ? error.message : 'Something went wrong while loading this page.';
  return (
    <div role="alert" className="rounded-lg border border-danger/30 bg-danger-soft px-5 py-4">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-foreground">
            {offline ? 'Backend not reachable' : notFound ? 'Not found' : 'Could not load data'}
          </p>
          <p className="mt-1 text-sm text-muted-foreground">{message}</p>
          {offline ? (
            <p className="mt-2 text-xs text-subtle-foreground">
              Start it from the repository root with{' '}
              <code className="rounded bg-surface-raised px-1 py-0.5 font-mono">
                pnpm dev:backend
              </code>
              .
            </p>
          ) : null}
        </div>
        {onRetry && !notFound ? (
          <Button size="sm" variant="ghost" onClick={onRetry}>
            <RotateCw aria-hidden /> Retry
          </Button>
        ) : null}
      </div>
    </div>
  );
}
