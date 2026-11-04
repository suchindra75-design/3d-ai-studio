import { AlertDialog as Primitive, Dialog as DialogPrimitive } from 'radix-ui';
import * as React from 'react';

import { cn } from '../lib/utils';
import { buttonVariants } from './button';

const overlayClass = 'fixed inset-0 z-50 bg-black/60';
const contentClass =
  'fixed left-1/2 top-1/2 z-50 w-[calc(100%-2rem)] max-w-md -translate-x-1/2 -translate-y-1/2 rounded-lg border border-border-strong bg-surface-raised p-6 shadow-2xl shadow-black/50';

interface ConfirmDialogProps {
  /** Element that opens the dialog. Omit when controlling `open` yourself. */
  trigger?: React.ReactNode;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  title: string;
  description: React.ReactNode;
  confirmLabel: string;
  destructive?: boolean;
  onConfirm: () => void;
}

/** Confirmation dialog for irreversible actions (delete project, delete photo…). */
export function ConfirmDialog({
  trigger,
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  destructive = false,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <Primitive.Root open={open} onOpenChange={onOpenChange}>
      {trigger ? <Primitive.Trigger asChild>{trigger}</Primitive.Trigger> : null}
      <Primitive.Portal>
        <Primitive.Overlay className={overlayClass} />
        <Primitive.Content className={contentClass}>
          <Primitive.Title className="text-base font-semibold">{title}</Primitive.Title>
          <Primitive.Description className="mt-2 text-sm text-muted-foreground">
            {description}
          </Primitive.Description>
          <div className="mt-6 flex justify-end gap-2">
            <Primitive.Cancel className={buttonVariants({ variant: 'ghost' })}>
              Cancel
            </Primitive.Cancel>
            <Primitive.Action
              className={buttonVariants({ variant: destructive ? 'destructive' : 'primary' })}
              onClick={onConfirm}
            >
              {confirmLabel}
            </Primitive.Action>
          </div>
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}

interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: React.ReactNode;
  children: React.ReactNode;
}

/** General modal dialog (forms). Focus is trapped and restored by Radix. */
export function Dialog({ open, onOpenChange, title, description, children }: DialogProps) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className={overlayClass} />
        <DialogPrimitive.Content className={cn(contentClass, 'max-w-lg')}>
          <DialogPrimitive.Title className="text-base font-semibold">{title}</DialogPrimitive.Title>
          {description ? (
            <DialogPrimitive.Description className="mt-1 text-sm text-muted-foreground">
              {description}
            </DialogPrimitive.Description>
          ) : (
            <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
          )}
          <div className="mt-5">{children}</div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
