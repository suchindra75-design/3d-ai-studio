import { Progress as ProgressPrimitive } from 'radix-ui';
import * as React from 'react';

import { cn } from '../lib/utils';

export function Progress({
  value,
  className,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root> & { value: number }) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <ProgressPrimitive.Root
      value={clamped}
      className={cn('relative h-1 w-full overflow-hidden rounded-full bg-surface-hover', className)}
      {...props}
    >
      <ProgressPrimitive.Indicator
        className="h-full bg-accent transition-[width] duration-200"
        style={{ width: `${clamped}%` }}
      />
    </ProgressPrimitive.Root>
  );
}

export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden
      className={cn('animate-pulse rounded-md bg-surface-hover', className)}
      {...props}
    />
  );
}

export function Spinner({ className, label = 'Loading' }: { className?: string; label?: string }) {
  return (
    <span
      role="status"
      aria-label={label}
      className={cn(
        'inline-block size-4 animate-spin rounded-full border-2 border-border-strong border-t-foreground',
        className,
      )}
    />
  );
}

const dotColors = {
  success: 'bg-success',
  warning: 'bg-warning',
  danger: 'bg-danger',
  neutral: 'bg-subtle-foreground',
} as const;

export function StatusDot({
  tone,
  className,
}: {
  tone: keyof typeof dotColors;
  className?: string;
}) {
  return (
    <span
      aria-hidden
      className={cn('inline-block size-2 rounded-full', dotColors[tone], className)}
    />
  );
}
