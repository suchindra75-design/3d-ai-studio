'use client';

/**
 * Structural editing tools for the workspace: arranging the model, part
 * actions, cutting with a plane (with a live preview in the 3D view), and
 * replacing textures with a UV layout view. (The cut preview inside the 3D
 * view is the editor's `ClipPreview`.) Every action is one undoable edit,
 * saved like any other as a new, server-validated version.
 */
import {
  axisVector,
  canTexture,
  centreOnOriginCommand,
  cutCommand,
  deleteCommand,
  isPbr,
  mirrorCommand,
  pivotToCentreCommand,
  placeOnGroundCommand,
  textureCommand,
  uvTriangles,
  type Axis,
  type Command,
  type CutSummary,
} from '@ai3d/editor';
import { Button, Switch, cn } from '@ai3d/ui';
import { formatCount, measure, sourceMaterials } from '@ai3d/viewer';
import { FlipHorizontal2, ImageUp, Scissors, Trash2 } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import * as THREE from 'three';

import { sizeLabel } from './scale';

/** Runs an edit; returns false (and shows why) when it cannot be done. */
export type RunEdit = (make: () => Command) => boolean;

const AXIS_NAMES: Axis[] = ['x', 'y', 'z'];

function MirrorButtons({ label, onMirror }: { label: string; onMirror: (axis: Axis) => void }) {
  return (
    <div className="flex items-center gap-1" role="group" aria-label={label}>
      <FlipHorizontal2 className="size-3.5 text-subtle-foreground" aria-hidden />
      <span className="mr-1 text-xs text-subtle-foreground">Mirror</span>
      {AXIS_NAMES.map((axis) => (
        <Button
          key={axis}
          size="sm"
          variant="secondary"
          className="h-7 px-2"
          aria-label={`Mirror along ${axis.toUpperCase()}`}
          onClick={() => onMirror(axis)}
        >
          {axis.toUpperCase()}
        </Button>
      ))}
    </div>
  );
}

/** Whole-model actions: ground, centre, mirror. */
export function ModelActions({ root, onEdit }: { root: THREE.Object3D; onEdit: RunEdit }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-1.5">
        <Button
          size="sm"
          variant="secondary"
          onClick={() => onEdit(() => placeOnGroundCommand(root))}
        >
          Place on ground
        </Button>
        <Button
          size="sm"
          variant="secondary"
          onClick={() => onEdit(() => centreOnOriginCommand(root))}
        >
          Centre on origin
        </Button>
      </div>
      <MirrorButtons
        label="Mirror the model"
        onMirror={(axis) => onEdit(() => mirrorCommand(root, root, axis))}
      />
    </div>
  );
}

/** Actions on the selected part. */
export function PartActions({
  root,
  part,
  onEdit,
  onDeleted,
}: {
  root: THREE.Object3D;
  part: THREE.Object3D;
  onEdit: RunEdit;
  onDeleted: () => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <MirrorButtons
        label="Mirror the part"
        onMirror={(axis) => onEdit(() => mirrorCommand(root, part, axis))}
      />
      <div className="flex flex-wrap gap-1.5">
        <Button
          size="sm"
          variant="secondary"
          onClick={() => onEdit(() => pivotToCentreCommand(part))}
        >
          Pivot to centre
        </Button>
        <Button
          size="sm"
          variant="ghost"
          className="text-danger"
          onClick={() => {
            if (onEdit(() => deleteCommand(part))) onDeleted();
          }}
        >
          <Trash2 aria-hidden /> Delete part
        </Button>
      </div>
    </div>
  );
}

// -- cutting ------------------------------------------------------------------------

export interface CutDraft {
  axis: Axis;
  /** Where the plane crosses the axis, in world units. */
  at: number;
  /** Keep the side the axis points to (above, right, front) or the other. */
  keepPositive: boolean;
  cap: boolean;
}

const SIDES: Record<Axis, [string, string]> = {
  x: ['Right (+X)', 'Left (−X)'],
  y: ['Above', 'Below'],
  z: ['Front (+Z)', 'Back (−Z)'],
};

export function draftPlane(draft: CutDraft): THREE.Plane {
  const normal = axisVector(draft.axis);
  return draft.keepPositive
    ? new THREE.Plane(normal, -draft.at)
    : new THREE.Plane(normal.negate(), draft.at);
}

export function defaultCut(root: THREE.Object3D, axis: Axis = 'y'): CutDraft {
  const box = measure(root);
  const i = AXIS_NAMES.indexOf(axis);
  const at = box.isEmpty() ? 0 : (box.min.getComponent(i) + box.max.getComponent(i)) / 2;
  return { axis, at, keepPositive: true, cap: true };
}

function describeSummary(summary: CutSummary): string {
  const parts: string[] = [];
  parts.push(`Cut ${summary.cut} part${summary.cut === 1 ? '' : 's'}`);
  if (summary.removed > 0) parts.push(`removed ${summary.removed} on the other side`);
  if (summary.capped > 0) {
    parts.push(`filled ${summary.capped} closed outline${summary.capped === 1 ? '' : 's'}`);
  }
  let text = `${parts.join(', ')}.`;
  if (summary.open > 0) {
    text += ` ${summary.open} outline${summary.open === 1 ? ' was' : 's were'} left open because the surface is not closed there.`;
  }
  if (summary.skipped.length > 0) {
    text += ` Not cut (skinned, instanced or with shape keys): ${summary.skipped.join(', ')}.`;
  }
  return text;
}

/** The plane to cut with, previewed live in the 3D view (see `ClipPreview`). */
export function CutPanel({
  root,
  draft,
  onDraft,
  onEdit,
  scaleKnown,
  revision,
}: {
  root: THREE.Object3D;
  draft: CutDraft;
  onDraft: (draft: CutDraft) => void;
  onEdit: RunEdit;
  scaleKnown: boolean;
  revision: number;
}) {
  const sliderId = useId();
  const [result, setResult] = useState<string | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `revision` signals in-place edits
  const box = useMemo(() => measure(root), [root, revision]);
  const i = AXIS_NAMES.indexOf(draft.axis);
  const min = box.isEmpty() ? 0 : box.min.getComponent(i);
  const max = box.isEmpty() ? 0 : box.max.getComponent(i);
  const step = Math.max((max - min) / 400, 1e-6);
  const [positive, negative] = SIDES[draft.axis];

  function cut() {
    let summary: CutSummary | null = null;
    const done = onEdit(() => {
      const outcome = cutCommand(
        root,
        draftPlane(draft),
        `Cut at ${draft.axis.toUpperCase()} = ${sizeLabel(draft.at, scaleKnown)}, kept ${(draft.keepPositive ? positive : negative).toLowerCase()}`,
        draft.cap,
      );
      summary = outcome.summary;
      return outcome.command;
    });
    setResult(done && summary ? describeSummary(summary) : null);
  }

  return (
    <div className="flex flex-col gap-4 px-4 py-4">
      <p className="text-xs leading-relaxed text-muted-foreground">
        The 3D view shows what will be kept. Cutting is one undoable edit; save it as a new version
        to keep it.
      </p>
      <div className="flex flex-col gap-1.5">
        <span className="text-xs text-subtle-foreground">Plane across</span>
        <div className="flex gap-1" role="radiogroup" aria-label="Cut across axis">
          {AXIS_NAMES.map((axis) => (
            <button
              key={axis}
              type="button"
              role="radio"
              aria-checked={draft.axis === axis}
              className={cn(
                'h-7 flex-1 rounded-md border text-xs font-medium',
                draft.axis === axis
                  ? 'border-accent bg-accent-soft text-foreground'
                  : 'border-border text-muted-foreground hover:text-foreground',
              )}
              onClick={() => onDraft({ ...defaultCut(root, axis), cap: draft.cap })}
            >
              {axis.toUpperCase()}
            </button>
          ))}
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between text-xs">
          <label htmlFor={sliderId} className="text-subtle-foreground">
            Position
          </label>
          <span className="tabular-nums text-muted-foreground">
            {draft.axis.toUpperCase()} = {sizeLabel(draft.at, scaleKnown)}
          </span>
        </div>
        <input
          id={sliderId}
          type="range"
          min={min}
          max={max}
          step={step}
          value={draft.at}
          className="w-full accent-accent"
          onChange={(event) => onDraft({ ...draft, at: Number(event.target.value) })}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <span className="text-xs text-subtle-foreground">Keep</span>
        <div className="flex gap-1" role="radiogroup" aria-label="Side to keep">
          {[true, false].map((keepPositive) => (
            <button
              key={String(keepPositive)}
              type="button"
              role="radio"
              aria-checked={draft.keepPositive === keepPositive}
              className={cn(
                'h-7 flex-1 rounded-md border text-xs font-medium',
                draft.keepPositive === keepPositive
                  ? 'border-accent bg-accent-soft text-foreground'
                  : 'border-border text-muted-foreground hover:text-foreground',
              )}
              onClick={() => onDraft({ ...draft, keepPositive })}
            >
              {keepPositive ? positive : negative}
            </button>
          ))}
        </div>
      </div>
      <label className="flex items-start justify-between gap-3 text-xs">
        <span className="flex flex-col gap-0.5">
          <span>Fill the cut</span>
          <span className="leading-relaxed text-subtle-foreground">
            Closes the opening with a flat face in the colours of the cut edge, so a closed model
            stays closed (needed for 3D printing).
          </span>
        </span>
        <Switch
          checked={draft.cap}
          onCheckedChange={(cap) => onDraft({ ...draft, cap })}
          aria-label="Fill the cut"
        />
      </label>
      <Button size="sm" variant="primary" onClick={cut}>
        <Scissors aria-hidden /> Cut
      </Button>
      {result ? (
        <p role="status" className="text-xs leading-relaxed text-success">
          {result}
        </p>
      ) : null}
    </div>
  );
}

// -- textures -----------------------------------------------------------------------

const UV_LIMIT = 150_000;

/** The material's texture with the selected parts' UV triangles drawn over it. */
export function UvLayout({
  root,
  material,
  revision,
}: {
  root: THREE.Object3D;
  material: THREE.Material;
  revision: number;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const layout = useMemo(
    () => uvTriangles(root, material, UV_LIMIT, sourceMaterials),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `revision` signals in-place edits
    [root, material, revision],
  );
  const map = isPbr(material) ? material.map : null;
  useEffect(() => {
    const element = canvas.current;
    const context = element?.getContext('2d');
    if (!element || !context) return;
    const size = element.width;
    context.fillStyle = '#1c1c20';
    context.fillRect(0, 0, size, size);
    const image = map?.image as CanvasImageSource | undefined;
    if (image) {
      try {
        context.drawImage(image, 0, 0, size, size);
      } catch {
        // Not drawable (e.g. a compressed texture): the UV lines are still shown.
      }
    }
    // glTF texture coordinates: (0, 0) is the top-left of the image.
    context.strokeStyle = 'rgba(143, 184, 255, 0.75)';
    context.lineWidth = 0.6;
    context.beginPath();
    const uv = layout.uv;
    for (let k = 0; k + 5 < uv.length; k += 6) {
      context.moveTo(uv[k]! * size, uv[k + 1]! * size);
      context.lineTo(uv[k + 2]! * size, uv[k + 3]! * size);
      context.lineTo(uv[k + 4]! * size, uv[k + 5]! * size);
      context.closePath();
    }
    context.stroke();
  }, [layout, map, revision]);
  return (
    <figure className="flex flex-col gap-1">
      <canvas
        ref={canvas}
        width={512}
        height={512}
        className="aspect-square w-full rounded border border-border"
        role="img"
        aria-label={`UV layout: ${formatCount(layout.total)} triangles`}
      />
      <figcaption className="text-[11px] text-subtle-foreground">
        {layout.total === 0
          ? 'These parts have no texture coordinates.'
          : layout.total > UV_LIMIT
            ? `Showing the first ${formatCount(UV_LIMIT)} of ${formatCount(layout.total)} triangles.`
            : `${formatCount(layout.total)} triangles.`}{' '}
        The square is the whole texture.
      </figcaption>
    </figure>
  );
}

/** "Replace texture…" / "Add texture…" with a UV layout toggle. */
export function TextureControls({
  root,
  material,
  onEdit,
  revision,
}: {
  root: THREE.Object3D;
  material: THREE.Material;
  onEdit: RunEdit;
  revision: number;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [showUv, setShowUv] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (!isPbr(material)) return null;
  const mapped = canTexture(root, material);
  const hasMap = material.map !== null;

  async function pick(file: File) {
    setProblem(null);
    if (!['image/png', 'image/jpeg'].includes(file.type)) {
      setProblem('Choose a PNG or JPEG image.');
      return;
    }
    let image: ImageBitmap;
    try {
      image = await createImageBitmap(file);
    } catch {
      setProblem('This image could not be read.');
      return;
    }
    if (!isPbr(material)) return;
    onEdit(() => textureCommand(material, image, file.name, file.type));
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <Button
          size="sm"
          variant="secondary"
          disabled={!mapped}
          title={mapped ? undefined : 'The parts using this material have no texture coordinates.'}
          onClick={() => input.current?.click()}
        >
          <ImageUp aria-hidden /> {hasMap ? 'Replace texture…' : 'Add texture…'}
        </Button>
        {mapped ? (
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={showUv}
            onClick={() => setShowUv(!showUv)}
          >
            {showUv ? 'Hide UV layout' : 'UV layout'}
          </Button>
        ) : null}
        <input
          ref={input}
          type="file"
          accept="image/png,image/jpeg"
          className="hidden"
          aria-label={`Texture image for ${material.name || 'this material'}`}
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = '';
            if (file) void pick(file);
          }}
        />
      </div>
      {!mapped ? (
        <p className="text-[11px] text-subtle-foreground">
          No texture can be added: the parts using this material have no texture coordinates (UVs).
        </p>
      ) : null}
      {problem ? (
        <p role="alert" className="text-xs text-danger">
          {problem}
        </p>
      ) : null}
      {showUv && mapped ? <UvLayout root={root} material={material} revision={revision} /> : null}
    </div>
  );
}
