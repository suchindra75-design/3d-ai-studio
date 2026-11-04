import type { CostEstimateRead } from '@ai3d/shared-types';

/** A fresh key per user action, so a double-click or network retry cannot start two jobs. */
export function newIdempotencyKey(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function formatEstimate(estimate: CostEstimateRead): string {
  const amount = `${estimate.amount.toLocaleString()} ${estimate.unit}`;
  if (estimate.usd === null) return `≈ ${amount}`;
  if (estimate.usd === 0) return 'Free';
  return `≈ ${amount} (US$${estimate.usd.toFixed(2)})`;
}
