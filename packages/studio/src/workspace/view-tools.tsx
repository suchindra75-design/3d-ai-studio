'use client';

/**
 * Viewing tools that never change the model: section view, measuring, screenshots
 * and comparing two versions side by side.
 */
import type { ModelVersionRead } from '@ai3d/shared-types';
import { Button, cn } from '@ai3d/ui';
import {
  DEFAULT_VIEWPORT_OPTIONS,
  Viewport,
  formatCount,
  measure,
  type CameraLink,
  type ViewRequest,
  type ViewportOptions,
} from '@ai3d/viewer';
import { X } from 'lucide-react';
import { useId, useMemo, useState } from 'react';
import * as THREE from 'three';

import { useApi, type DesktopFeatures } from '../context';
import { reportStatusLabel, versionSourceLabel } from '../labels';
import { useModelFile } from './model-file';
import { scaleOf, sizeLabel } from './scale';

type Axis = 'x' | 'y' | 'z';
const AXES: Axis[] = ['x', 'y', 'z'];

const floating =
  'pointer-events-auto rounded-md border border-border bg-surface/95 p-2.5 shadow-sm';

// -- section ------------------------------------------------------------------------

export interface SectionState {
  axis: Axis;
  at: number;
  /** Show the side the axis points to (true) or the other side. */
  keepPositive: boolean;
}

export function sectionPlane(section: SectionState): THREE.Plane {
  const normal = new THREE.Vector3().setComponent(AXES.indexOf(section.axis), 1);
  return section.keepPositive
    ? new THREE.Plane(normal, -section.at)
    : new THREE.Plane(normal.negate(), section.at);
}

export function defaultSection(root: THREE.Object3D, axis: Axis = 'x'): SectionState {
  const box = measure(root);
  const i = AXES.indexOf(axis);
  const at = box.isEmpty() ? 0 : (box.min.getComponent(i) + box.max.getComponent(i)) / 2;
  return { axis, at, keepPositive: false };
}

export function SectionControls({
  root,
  section,
  onSection,
  scaleKnown,
  revision,
}: {
  root: THREE.Object3D;
  section: SectionState;
  onSection: (section: SectionState) => void;
  scaleKnown: boolean;
  revision: number;
}) {
  const id = useId();
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `revision` signals in-place edits
  const box = useMemo(() => measure(root), [root, revision]);
  const i = AXES.indexOf(section.axis);
  const min = box.isEmpty() ? 0 : box.min.getComponent(i);
  const max = box.isEmpty() ? 0 : box.max.getComponent(i);
  return (
    <div className={cn(floating, 'flex w-56 flex-col gap-2 text-xs')} aria-label="Section view">
      <div className="flex items-center justify-between">
        <span className="font-medium">Section</span>
        <span className="text-[11px] text-subtle-foreground">view only</span>
      </div>
      <div className="flex gap-1" role="radiogroup" aria-label="Section across axis">
        {AXES.map((axis) => (
          <button
            key={axis}
            type="button"
            role="radio"
            aria-checked={section.axis === axis}
            className={cn(
              'h-6 flex-1 rounded border text-[11px] font-medium',
              section.axis === axis
                ? 'border-accent bg-accent-soft'
                : 'border-border text-muted-foreground',
            )}
            onClick={() =>
              onSection({ ...defaultSection(root, axis), keepPositive: section.keepPositive })
            }
          >
            {axis.toUpperCase()}
          </button>
        ))}
        <button
          type="button"
          className="h-6 rounded border border-border px-2 text-[11px] text-muted-foreground"
          aria-label="Show the other side"
          onClick={() => onSection({ ...section, keepPositive: !section.keepPositive })}
        >
          Flip
        </button>
      </div>
      <label htmlFor={id} className="flex justify-between text-[11px] text-subtle-foreground">
        <span>Position</span>
        <span className="tabular-nums">
          {section.axis.toUpperCase()} = {sizeLabel(section.at, scaleKnown)}
        </span>
      </label>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={Math.max((max - min) / 400, 1e-6)}
        value={section.at}
        className="w-full accent-accent"
        onChange={(event) => onSection({ ...section, at: Number(event.target.value) })}
      />
    </div>
  );
}

// -- measuring ----------------------------------------------------------------------

export function distanceLabel(points: THREE.Vector3[], scaleKnown: boolean): string | null {
  const [a, b] = points;
  return a && b ? sizeLabel(a.distanceTo(b), scaleKnown) : null;
}

export function MeasurePanel({
  points,
  scaleKnown,
  measured,
  onClear,
}: {
  points: THREE.Vector3[];
  scaleKnown: boolean;
  /** The surface was measured (photogrammetry) rather than invented by AI. */
  measured: boolean;
  onClear: () => void;
}) {
  const [a, b] = points;
  const delta = a && b ? b.clone().sub(a) : null;
  return (
    <div className={cn(floating, 'flex w-56 flex-col gap-1.5 text-xs')} aria-label="Measurement">
      <div className="flex items-center justify-between">
        <span className="font-medium">Measure</span>
        {points.length > 0 ? (
          <button
            type="button"
            className="text-[11px] text-muted-foreground hover:text-foreground"
            onClick={onClear}
          >
            Clear
          </button>
        ) : null}
      </div>
      {delta ? (
        <>
          <p className="text-sm font-medium tabular-nums" data-testid="measured-distance">
            {distanceLabel(points, scaleKnown)}
          </p>
          <p className="text-[11px] tabular-nums text-subtle-foreground">
            X {sizeLabel(Math.abs(delta.x), scaleKnown)} · Y{' '}
            {sizeLabel(Math.abs(delta.y), scaleKnown)} · Z{' '}
            {sizeLabel(Math.abs(delta.z), scaleKnown)}
          </p>
        </>
      ) : (
        <p className="text-[11px] text-subtle-foreground">
          {points.length === 0 ? 'Click a first point on the model.' : 'Click a second point.'}
        </p>
      )}
      <p className="text-[11px] leading-relaxed text-subtle-foreground">
        {scaleKnown
          ? 'In real-world units from your measurement; only as accurate as that measurement.'
          : measured
            ? 'In the model’s own units: the scale is unknown until you set it in Enhance.'
            : 'In the model’s own units. This model is AI-generated: its proportions are not measurements.'}
      </p>
    </div>
  );
}

// -- screenshots --------------------------------------------------------------------

/** Saves a PNG: a download in the browser, the native Save dialog in the desktop app. */
export async function saveScreenshot(
  blob: Blob,
  fileName: string,
  desktop: DesktopFeatures | undefined,
): Promise<string | null> {
  if (desktop?.saveImage) {
    return desktop.saveImage(new Uint8Array(await blob.arrayBuffer()), fileName);
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return fileName;
}

// -- comparing versions -------------------------------------------------------------

function facts(version: ModelVersionRead): Record<string, string> {
  const m = (version.quality_report?.metrics ?? {}) as Record<string, unknown>;
  const scale = scaleOf(version) !== null;
  const size = Array.isArray(m.bbox_size) ? (m.bbox_size as number[]) : null;
  const yesNo = (value: unknown) => (value === true ? 'Yes' : value === false ? 'No' : '—');
  return {
    Source: versionSourceLabel[version.source],
    Triangles: typeof m.triangle_count === 'number' ? formatCount(m.triangle_count) : '—',
    Vertices: typeof m.vertex_count === 'number' ? formatCount(m.vertex_count) : '—',
    Size: size ? size.map((v) => sizeLabel(v, scale)).join(' × ') : '—',
    Watertight: yesNo(m.watertight),
    Textures: typeof m.texture_count === 'number' ? String(m.texture_count) : '—',
    'File checks': version.quality_report ? reportStatusLabel[version.quality_report.status] : '—',
  };
}

/** The stored facts of two versions side by side; differing rows are marked. */
export function CompareTable({ left, right }: { left: ModelVersionRead; right: ModelVersionRead }) {
  const a = facts(left);
  const b = facts(right);
  return (
    <table className="w-full text-xs" aria-label="Version comparison">
      <thead>
        <tr className="text-left text-subtle-foreground">
          <th className="py-1 font-normal" />
          <th className="py-1 font-medium">Version {left.version_number}</th>
          <th className="py-1 font-medium">Version {right.version_number}</th>
        </tr>
      </thead>
      <tbody>
        {Object.keys(a).map((key) => {
          const differs = a[key] !== b[key];
          return (
            <tr key={key} className={cn('border-t border-border', differs && 'text-foreground')}>
              <th scope="row" className="py-1 pr-2 text-left font-normal text-subtle-foreground">
                {key}
              </th>
              <td className="py-1 tabular-nums">{a[key]}</td>
              <td className={cn('py-1 tabular-nums', differs && 'font-medium text-accent')}>
                {b[key]}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** The other version's 3D view, sharing the camera with the main view. */
export function CompareViewport({
  version,
  options,
  view,
  link,
  onClose,
}: {
  version: ModelVersionRead;
  options: ViewportOptions;
  view: ViewRequest;
  link: CameraLink;
  onClose: () => void;
}) {
  const api = useApi();
  const file = useModelFile(api, version.file_url);
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <div className="relative size-full">
      {file.status === 'ready' ? (
        <Viewport
          className="size-full"
          model={file.model.root}
          revision={0}
          options={{ ...DEFAULT_VIEWPORT_OPTIONS, ...options, bounds: options.bounds }}
          selectedId={selected}
          onSelect={setSelected}
          view={view}
          cameraLink={link}
          label={`3D view of version ${version.version_number}, for comparison`}
        />
      ) : (
        <p className="flex h-full items-center justify-center text-xs text-muted-foreground">
          {file.status === 'error' ? file.message : 'Opening the 3D file…'}
        </p>
      )}
      <div className="pointer-events-none absolute inset-x-2 top-2 flex items-start justify-between">
        <span className="rounded bg-background/80 px-2 py-1 text-[11px] font-medium">
          Version {version.version_number} · {versionSourceLabel[version.source]}
        </span>
        <Button
          size="sm"
          variant="secondary"
          className="pointer-events-auto"
          aria-label="Stop comparing"
          onClick={onClose}
        >
          <X aria-hidden /> Close
        </Button>
      </div>
    </div>
  );
}
