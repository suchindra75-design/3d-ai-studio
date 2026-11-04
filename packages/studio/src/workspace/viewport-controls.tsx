'use client';

import type { GizmoMode } from '@ai3d/editor';
import { cn } from '@ai3d/ui';
import {
  CAMERA_PRESETS,
  LIGHTING_PRESETS,
  type CameraPreset,
  type DisplayMode,
  type LightingPreset,
  type ViewportOptions,
} from '@ai3d/viewer';
import { Camera, Focus } from 'lucide-react';
import type { ReactNode } from 'react';

const panel = 'rounded-md border border-border bg-surface/95 p-0.5 shadow-sm';

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { value: T; label: string; hint?: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div role="radiogroup" aria-label={label} className={cn(panel, 'flex')}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          title={option.hint}
          className={cn(
            'h-7 rounded px-2.5 text-xs font-medium transition-colors',
            value === option.value
              ? 'bg-surface-hover text-foreground'
              : 'text-muted-foreground hover:text-foreground',
          )}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function Toggle({
  pressed,
  onChange,
  children,
}: {
  pressed: boolean;
  onChange: (pressed: boolean) => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      className={cn(
        'h-7 rounded px-2.5 text-xs font-medium transition-colors',
        pressed
          ? 'bg-surface-hover text-foreground'
          : 'text-muted-foreground hover:text-foreground',
      )}
      onClick={() => onChange(!pressed)}
    >
      {children}
    </button>
  );
}

const DISPLAY: { value: DisplayMode; label: string; hint?: string }[] = [
  { value: 'material', label: 'Material' },
  { value: 'solid', label: 'Solid' },
  { value: 'wireframe', label: 'Wireframe' },
  { value: 'normals', label: 'Normals', hint: 'Surface direction as colour' },
  { value: 'uv', label: 'UV', hint: 'Checker texture: shows stretching and seams' },
];

const GIZMO: { value: GizmoMode; label: string; hint: string }[] = [
  { value: 'translate', label: 'Move', hint: 'Move (W)' },
  { value: 'rotate', label: 'Rotate', hint: 'Rotate (E)' },
  { value: 'scale', label: 'Scale', hint: 'Scale (R)' },
];

export function ViewportControls({
  options,
  onOptions,
  onView,
  section,
  onSection,
  measuring,
  onMeasuring,
  onScreenshot,
}: {
  options: ViewportOptions;
  /** Receives only the changed setting, so quick successive clicks never undo each other. */
  onOptions: (change: Partial<ViewportOptions>) => void;
  onView: (preset: CameraPreset) => void;
  section: boolean;
  onSection: (on: boolean) => void;
  measuring: boolean;
  onMeasuring: (on: boolean) => void;
  onScreenshot: () => void;
}) {
  const set = <K extends keyof ViewportOptions>(key: K, value: ViewportOptions[K]) =>
    onOptions({ [key]: value });
  return (
    <div className="pointer-events-none absolute inset-x-2 top-2 flex flex-wrap items-start justify-between gap-2">
      <div className="pointer-events-auto flex flex-wrap gap-2">
        <Segmented
          label="Display mode"
          value={options.display}
          options={DISPLAY}
          onChange={(value) => set('display', value)}
        />
        <div role="group" aria-label="Scene helpers" className={cn(panel, 'flex')}>
          <Toggle pressed={options.grid} onChange={(v) => set('grid', v)}>
            Grid
          </Toggle>
          <Toggle pressed={options.axes} onChange={(v) => set('axes', v)}>
            Axes
          </Toggle>
          <Toggle pressed={options.bounds} onChange={(v) => set('bounds', v)}>
            Bounds
          </Toggle>
          <Toggle pressed={options.shadows} onChange={(v) => set('shadows', v)}>
            Shadows
          </Toggle>
        </div>
        <div role="group" aria-label="View tools" className={cn(panel, 'flex')}>
          <Toggle pressed={section} onChange={onSection}>
            Section
          </Toggle>
          <Toggle pressed={measuring} onChange={onMeasuring}>
            Measure
          </Toggle>
        </div>
        <label className={cn(panel, 'flex items-center gap-1 pl-2 text-xs text-muted-foreground')}>
          Light
          <select
            aria-label="Lighting"
            className="h-7 rounded bg-transparent px-1 text-xs text-foreground outline-none"
            value={options.lighting}
            onChange={(event) => set('lighting', event.target.value as LightingPreset)}
          >
            {LIGHTING_PRESETS.map((preset) => (
              <option key={preset.value} value={preset.value} title={preset.hint}>
                {preset.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className={cn(panel, 'pointer-events-auto flex items-center')}>
        <button
          type="button"
          className="flex h-7 items-center gap-1.5 rounded px-2.5 text-xs font-medium text-muted-foreground hover:text-foreground"
          title="Save the view as a PNG"
          onClick={onScreenshot}
        >
          <Camera className="size-3.5" aria-hidden /> Screenshot
        </button>
        <button
          type="button"
          className="flex h-7 items-center gap-1.5 rounded px-2.5 text-xs font-medium text-muted-foreground hover:text-foreground"
          title="Fit model in view (F)"
          onClick={() => onView('iso')}
        >
          <Focus className="size-3.5" aria-hidden /> Fit
        </button>
        <select
          aria-label="Camera view"
          className="h-7 rounded bg-transparent px-1.5 text-xs text-muted-foreground outline-none hover:text-foreground"
          value=""
          onChange={(event) => {
            if (event.target.value) onView(event.target.value as CameraPreset);
          }}
        >
          <option value="">View…</option>
          {CAMERA_PRESETS.map((preset) => (
            <option key={preset.value} value={preset.value}>
              {preset.label} ({preset.key})
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

export function GizmoControls({
  mode,
  onMode,
}: {
  mode: GizmoMode;
  onMode: (mode: GizmoMode) => void;
}) {
  return (
    <div className="pointer-events-auto">
      <Segmented label="Transform tool" value={mode} options={GIZMO} onChange={onMode} />
    </div>
  );
}
