import { cn } from '@ai3d/ui';

/** Wireframe cube mark + wordmark. */
export function LogoMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden className={cn('size-5', className)}>
      <path
        d="M12 2.5 20.5 7.25v9.5L12 21.5l-8.5-4.75v-9.5L12 2.5Z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="M3.5 7.25 12 12m0 0 8.5-4.75M12 12v9.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
        opacity="0.55"
      />
    </svg>
  );
}

export function Logo({ className }: { className?: string }) {
  return (
    <span
      className={cn('flex items-center gap-2 text-[15px] font-semibold tracking-tight', className)}
    >
      <LogoMark />
      AI 3D Studio
    </span>
  );
}
