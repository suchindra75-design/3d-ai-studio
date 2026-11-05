'use client';

/**
 * Colour adjustments (texture or vertex colours) and texture tiling for one
 * material. Colours preview live (textures at reduced resolution while
 * dragging); Apply records one undoable edit at full resolution.
 */
import {
  EditError,
  NO_ADJUST,
  colourAdjustCommand,
  colourTarget,
  isNeutral,
  isPbr,
  meshesUsing,
  tilingCommand,
  type ColourAdjust,
  type Command,
} from '@ai3d/editor';
import { Button } from '@ai3d/ui';
import { sourceMaterials } from '@ai3d/viewer';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type * as THREE from 'three';

import type { RunEdit } from './edit-tools';

const PREVIEW_SIZE = 512;
// Which materials' panels are open. The material editor is re-created after every
// edit, so the panel would otherwise close each time something is applied.
const openPanels = new Set<string>();

const SLIDERS: {
  key: keyof ColourAdjust;
  label: string;
  min: number;
  max: number;
  step: number;
}[] = [
  { key: 'brightness', label: 'Brightness', min: -1, max: 1, step: 0.01 },
  { key: 'contrast', label: 'Contrast', min: -1, max: 1, step: 0.01 },
  { key: 'saturation', label: 'Saturation', min: -1, max: 1, step: 0.01 },
  { key: 'hue', label: 'Hue (°)', min: -180, max: 180, step: 1 },
];

function NumberField({
  label,
  value,
  onChange,
  step,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  step: number;
}) {
  const id = useId();
  return (
    <label htmlFor={id} className="flex flex-col gap-0.5 text-[11px] text-subtle-foreground">
      {label}
      <input
        id={id}
        type="number"
        step={step}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-7 w-full rounded-md border border-border bg-background px-1.5 text-xs text-foreground tabular-nums outline-none focus:border-accent"
      />
    </label>
  );
}

export function AdjustControls({
  root,
  material,
  onEdit,
  onPreview,
  revision,
}: {
  root: THREE.Object3D;
  material: THREE.Material;
  onEdit: RunEdit;
  onPreview: () => void;
  revision: number;
}) {
  const meshes = useMemo(
    () => meshesUsing(root, material, sourceMaterials),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-read after edits
    [root, material, revision],
  );
  const target = colourTarget(material, meshes);
  const [adjust, setAdjust] = useState<ColourAdjust>(NO_ADJUST);
  const [tiling, setTiling] = useState({ ru: '1', rv: '1', ou: '0', ov: '0', rot: '0' });
  const preview = useRef<Command | null>(null);
  const tileable =
    isPbr(material) && material.map !== null && meshes.some((m) => m.geometry.getAttribute('uv'));

  function clearPreview() {
    preview.current?.revert();
    preview.current = null;
  }

  // Undo the preview when the panel goes away (another part selected, tab changed).
  useEffect(() => clearPreview, []);

  if (!isPbr(material) || (target === null && !tileable)) return null;

  function change(next: ColourAdjust) {
    setAdjust(next);
    clearPreview();
    if (!isPbr(material) || isNeutral(next)) {
      onPreview();
      return;
    }
    try {
      const command = colourAdjustCommand(material, meshes, next, PREVIEW_SIZE);
      command.apply();
      preview.current = command;
    } catch (cause) {
      if (!(cause instanceof EditError)) throw cause;
    }
    onPreview();
  }

  function applyColours() {
    clearPreview();
    if (!isPbr(material)) return;
    if (onEdit(() => colourAdjustCommand(material, meshes, adjust))) setAdjust(NO_ADJUST);
  }

  function applyTiling() {
    if (!isPbr(material)) return;
    const n = (text: string) => Number(text.trim());
    const value = {
      repeat: [n(tiling.ru), n(tiling.rv)] as [number, number],
      offset: [n(tiling.ou), n(tiling.ov)] as [number, number],
      rotation: n(tiling.rot),
    };
    if ([...value.repeat, ...value.offset, value.rotation].some((v) => !Number.isFinite(v))) return;
    if (onEdit(() => tilingCommand(material, meshes, value))) {
      setTiling({ ru: '1', rv: '1', ou: '0', ov: '0', rot: '0' });
    }
  }

  return (
    <details
      className="text-xs"
      open={openPanels.has(material.uuid)}
      onToggle={(event) => {
        if (event.currentTarget.open) openPanels.add(material.uuid);
        else openPanels.delete(material.uuid);
      }}
    >
      <summary className="cursor-pointer text-muted-foreground">Adjust colours and tiling</summary>
      <div className="mt-2 flex flex-col gap-3">
        {target ? (
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-[11px] text-subtle-foreground">
              {target === 'texture' ? 'Texture colours' : 'Vertex colours'}
            </legend>
            {SLIDERS.map(({ key, label, min, max, step }) => (
              <label key={key} className="flex flex-col gap-0.5">
                <span className="flex justify-between text-[11px] text-subtle-foreground">
                  {label}
                  <span className="tabular-nums">
                    {key === 'hue' ? Math.round(adjust[key]) : adjust[key].toFixed(2)}
                  </span>
                </span>
                <input
                  type="range"
                  min={min}
                  max={max}
                  step={step}
                  value={adjust[key]}
                  aria-label={label}
                  className="w-full accent-accent"
                  onChange={(event) => change({ ...adjust, [key]: Number(event.target.value) })}
                />
              </label>
            ))}
            <div className="flex gap-1.5">
              <Button
                size="sm"
                variant="secondary"
                disabled={isNeutral(adjust)}
                onClick={applyColours}
              >
                Apply colours
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={isNeutral(adjust)}
                onClick={() => change(NO_ADJUST)}
              >
                Reset
              </Button>
            </div>
            {!isNeutral(adjust) ? (
              <p className="text-[11px] text-warning">
                Preview only{target === 'texture' ? ' (reduced resolution)' : ''}: apply it to make
                it an edit.
              </p>
            ) : null}
          </fieldset>
        ) : null}
        {tileable ? (
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-[11px] text-subtle-foreground">
              Tiling (changes the texture coordinates)
            </legend>
            <div className="grid grid-cols-3 gap-1.5">
              <NumberField
                label="Repeat U"
                step={0.5}
                value={tiling.ru}
                onChange={(ru) => setTiling({ ...tiling, ru })}
              />
              <NumberField
                label="Repeat V"
                step={0.5}
                value={tiling.rv}
                onChange={(rv) => setTiling({ ...tiling, rv })}
              />
              <NumberField
                label="Rotation (°)"
                step={15}
                value={tiling.rot}
                onChange={(rot) => setTiling({ ...tiling, rot })}
              />
              <NumberField
                label="Offset U"
                step={0.05}
                value={tiling.ou}
                onChange={(ou) => setTiling({ ...tiling, ou })}
              />
              <NumberField
                label="Offset V"
                step={0.05}
                value={tiling.ov}
                onChange={(ov) => setTiling({ ...tiling, ov })}
              />
            </div>
            <Button size="sm" variant="secondary" onClick={applyTiling}>
              Apply tiling
            </Button>
          </fieldset>
        ) : null}
      </div>
    </details>
  );
}
