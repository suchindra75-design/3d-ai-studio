'use client';

import { cn } from '@ai3d/ui';
import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';

export type Vec3 = [number, number, number];

const AXES = ['X', 'Y', 'Z'] as const;

function format(value: number): string {
  return String(Number(value.toFixed(4)));
}

/**
 * Three numeric inputs. Typing only edits a draft; Enter or leaving the field
 * commits it as one edit, Escape discards it.
 */
export function Vec3Field({
  label,
  value,
  step,
  onCommit,
}: {
  label: string;
  value: Vec3;
  step: number;
  onCommit: (value: Vec3) => void;
}) {
  const [draft, setDraft] = useState<string[] | null>(null);
  const shown = draft ?? value.map(format);

  function commit() {
    if (!draft) return;
    setDraft(null);
    const parsed = draft.map((text) => Number(text.trim()));
    if (!parsed.every(Number.isFinite)) return;
    if (parsed.some((v, i) => Math.abs(v - value[i]!) > 1e-9)) onCommit(parsed as Vec3);
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Enter') commit();
    if (event.key === 'Escape') setDraft(null);
  }

  return (
    <fieldset className="flex flex-col gap-1">
      <legend className="mb-1 text-xs text-subtle-foreground">{label}</legend>
      <div className="grid grid-cols-3 gap-1.5">
        {AXES.map((axis, i) => (
          <label
            key={axis}
            className="flex h-8 items-center gap-1 rounded-md border border-border bg-background px-2 focus-within:border-accent"
          >
            <span className="text-[11px] font-medium text-subtle-foreground" aria-hidden>
              {axis}
            </span>
            <input
              type="number"
              inputMode="decimal"
              step={step}
              aria-label={`${label} ${axis}`}
              className="w-full min-w-0 bg-transparent text-xs tabular-nums outline-none"
              value={shown[i]}
              onChange={(event) =>
                setDraft((current) => {
                  const next = [...(current ?? value.map(format))];
                  next[i] = event.target.value;
                  return next;
                })
              }
              onBlur={commit}
              onKeyDown={onKeyDown}
            />
          </label>
        ))}
      </div>
    </fieldset>
  );
}

/**
 * A 0–1 slider that previews while it moves and commits one edit when released
 * (pointer up, key up or blur).
 */
export function LiveSlider({
  label,
  value,
  onStart,
  onPreview,
  onCommit,
}: {
  label: string;
  value: number;
  onStart: () => void;
  onPreview: (value: number) => void;
  onCommit: (value: number) => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState<number | null>(null);
  const shown = draft ?? value;

  function end() {
    if (draft === null) return;
    setDraft(null);
    onCommit(draft);
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between text-xs">
        <label htmlFor={id} className="text-subtle-foreground">
          {label}
        </label>
        <span className="tabular-nums text-muted-foreground">{shown.toFixed(2)}</span>
      </div>
      <input
        id={id}
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={shown}
        className="w-full accent-accent"
        onChange={(event) => {
          const next = Number(event.target.value);
          if (draft === null) onStart();
          setDraft(next);
          onPreview(next);
        }}
        onPointerUp={end}
        onKeyUp={end}
        onBlur={end}
      />
    </div>
  );
}

/** Native colour picker: previews while picking, commits when the picker closes. */
export function ColorField({
  label,
  value,
  onStart,
  onPreview,
  onCommit,
}: {
  label: string;
  value: string;
  onStart: () => void;
  onPreview: (hex: string) => void;
  onCommit: (hex: string) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [draft, setDraft] = useState<string | null>(null);
  // The native `change` event fires once, when the user confirms a colour.
  const latest = useRef({ draft, onCommit });
  useEffect(() => {
    latest.current = { draft, onCommit };
  });
  useEffect(() => {
    const element = input.current;
    if (!element) return undefined;
    const commit = () => {
      const { draft: pending, onCommit: done } = latest.current;
      if (pending === null) return;
      setDraft(null);
      done(pending);
    };
    element.addEventListener('change', commit);
    return () => element.removeEventListener('change', commit);
  }, []);

  return (
    <label className="flex items-center justify-between gap-2 text-xs">
      <span className="text-subtle-foreground">{label}</span>
      <span className="flex items-center gap-2">
        <span className="font-mono tabular-nums text-muted-foreground">{draft ?? value}</span>
        <input
          ref={input}
          type="color"
          value={draft ?? value}
          className={cn(
            'h-7 w-10 cursor-pointer rounded border border-border-strong bg-transparent p-0.5',
          )}
          onInput={(event) => {
            const next = event.currentTarget.value;
            if (draft === null) onStart();
            setDraft(next);
            onPreview(next);
          }}
          onChange={() => {
            // Handled by the native listener above; React's onChange fires on every input.
          }}
        />
      </span>
    </label>
  );
}
