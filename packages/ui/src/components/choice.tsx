import { RadioGroup, Switch as SwitchPrimitive } from 'radix-ui';
import * as React from 'react';

import { cn } from '../lib/utils';

export interface ChoiceOption<T extends string> {
  value: T;
  label: string;
  description?: string;
  icon?: React.ReactNode;
}

interface ChoiceCardsProps<T extends string> {
  value: T;
  onValueChange: (value: T) => void;
  options: ChoiceOption<T>[];
  name: string;
  className?: string;
  'aria-labelledby'?: string;
}

/** A radio group rendered as selectable cards (keyboard: arrow keys). */
export function ChoiceCards<T extends string>({
  value,
  onValueChange,
  options,
  name,
  className,
  ...aria
}: ChoiceCardsProps<T>) {
  return (
    <RadioGroup.Root
      value={value}
      onValueChange={(v) => onValueChange(v as T)}
      name={name}
      className={cn('grid gap-2', className)}
      {...aria}
    >
      {options.map((option) => (
        <RadioGroup.Item
          key={option.value}
          value={option.value}
          className={cn(
            'group flex cursor-pointer gap-3 rounded-md border border-border-strong bg-surface p-3.5 text-left transition-colors',
            'hover:bg-surface-hover data-[state=checked]:border-accent data-[state=checked]:bg-accent-soft',
          )}
        >
          {option.icon ? (
            <span className="mt-0.5 text-muted-foreground group-data-[state=checked]:text-accent [&_svg]:size-5">
              {option.icon}
            </span>
          ) : null}
          <span className="flex flex-col gap-0.5">
            <span className="text-sm font-medium text-foreground">{option.label}</span>
            {option.description ? (
              <span className="text-xs leading-relaxed text-muted-foreground">
                {option.description}
              </span>
            ) : null}
          </span>
        </RadioGroup.Item>
      ))}
    </RadioGroup.Root>
  );
}

export function Switch({ className, ...props }: React.ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border border-border-strong bg-surface-hover transition-colors data-[state=checked]:border-accent data-[state=checked]:bg-accent disabled:opacity-50',
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb className="block size-4 translate-x-0.5 rounded-full bg-foreground shadow transition-transform data-[state=checked]:translate-x-[17px]" />
    </SwitchPrimitive.Root>
  );
}
