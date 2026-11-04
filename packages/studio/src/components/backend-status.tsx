'use client';

import { StatusDot } from '@ai3d/ui';

import { useHealth } from '../api/hooks';

export function BackendStatus({ compact = false }: { compact?: boolean }) {
  const health = useHealth();
  const tone = health.isPending
    ? 'neutral'
    : health.data?.status === 'ok'
      ? 'success'
      : health.data
        ? 'warning'
        : 'danger';
  const label = health.isPending
    ? 'Connecting…'
    : health.data?.status === 'ok'
      ? `Backend v${health.data.version}`
      : health.data
        ? 'Backend degraded'
        : 'Backend offline';
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center gap-2 text-xs text-subtle-foreground"
      title={label}
    >
      <StatusDot tone={tone} />
      {compact ? <span className="sr-only">{label}</span> : <span>{label}</span>}
    </div>
  );
}
