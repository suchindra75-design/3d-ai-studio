import { DropdownMenu as Primitive } from 'radix-ui';
import * as React from 'react';

import { cn } from '../lib/utils';

export const DropdownMenu = Primitive.Root;
export const DropdownMenuTrigger = Primitive.Trigger;

export function DropdownMenuContent({
  className,
  sideOffset = 6,
  ...props
}: React.ComponentProps<typeof Primitive.Content>) {
  return (
    <Primitive.Portal>
      <Primitive.Content
        sideOffset={sideOffset}
        className={cn(
          'z-50 min-w-44 rounded-md border border-border-strong bg-surface-raised p-1 shadow-xl shadow-black/40',
          className,
        )}
        {...props}
      />
    </Primitive.Portal>
  );
}

export function DropdownMenuItem({
  className,
  destructive,
  ...props
}: React.ComponentProps<typeof Primitive.Item> & { destructive?: boolean }) {
  return (
    <Primitive.Item
      className={cn(
        'flex cursor-pointer select-none items-center gap-2 rounded-sm px-2 py-1.5 text-[13px] outline-none data-[highlighted]:bg-surface-hover [&_svg]:size-4',
        destructive ? 'text-danger' : 'text-foreground',
        className,
      )}
      {...props}
    />
  );
}

export function DropdownMenuSeparator() {
  return <Primitive.Separator className="my-1 h-px bg-border" />;
}
