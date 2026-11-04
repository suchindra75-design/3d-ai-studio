/** Edit commands for transforms and material properties. */
import * as THREE from 'three';

import type { Command } from './history';

type Vec3 = [number, number, number];

export interface TransformState {
  position: Vec3;
  quaternion: [number, number, number, number];
  scale: Vec3;
}

export function captureTransform(object: THREE.Object3D): TransformState {
  return {
    position: object.position.toArray() as Vec3,
    quaternion: object.quaternion.toArray() as TransformState['quaternion'],
    scale: object.scale.toArray() as Vec3,
  };
}

export function applyTransform(object: THREE.Object3D, state: TransformState): void {
  object.position.fromArray(state.position);
  object.quaternion.fromArray(state.quaternion);
  object.scale.fromArray(state.scale);
  object.updateMatrixWorld(true);
}

function close(a: readonly number[], b: readonly number[], epsilon = 1e-9): boolean {
  return a.every((value, i) => Math.abs(value - (b[i] ?? Number.NaN)) <= epsilon);
}

export function sameTransform(a: TransformState, b: TransformState): boolean {
  return (
    close(a.position, b.position) && close(a.quaternion, b.quaternion) && close(a.scale, b.scale)
  );
}

/** "Moved", "Rotated", "Scaled" or "Transformed" (several at once). */
export function describeTransform(name: string, before: TransformState, after: TransformState) {
  const changed = [
    !close(before.position, after.position) && 'Moved',
    !close(before.quaternion, after.quaternion) && 'Rotated',
    !close(before.scale, after.scale) && 'Scaled',
  ].filter((verb): verb is string => Boolean(verb));
  const verb = changed.length === 1 ? changed[0] : 'Transformed';
  return `${verb} "${name}"`;
}

export function transformCommand(
  object: THREE.Object3D,
  before: TransformState,
  after: TransformState,
  label: string,
): Command {
  return {
    label,
    apply: () => applyTransform(object, after),
    revert: () => applyTransform(object, before),
  };
}

// -- materials ------------------------------------------------------------------

export type ColorMaterial = THREE.Material & { color: THREE.Color };
export type PbrMaterial = THREE.MeshStandardMaterial;

export function hasColor(material: THREE.Material): material is ColorMaterial {
  return (material as Partial<ColorMaterial>).color instanceof THREE.Color;
}

/** MeshStandardMaterial and MeshPhysicalMaterial (what glTF PBR materials load as). */
export function isPbr(material: THREE.Material): material is PbrMaterial {
  return (material as THREE.MeshStandardMaterial).isMeshStandardMaterial === true;
}

export function materialName(material: THREE.Material): string {
  return material.name.trim() || 'Unnamed material';
}

/** Colours are edited as sRGB hex, the way people pick them. */
export function colorHex(material: ColorMaterial): string {
  return `#${material.color.getHexString(THREE.SRGBColorSpace)}`;
}

/**
 * ``from`` is the colour before a live preview started (inputs preview while
 * the user drags, then record one command); it defaults to the current colour.
 */
export function colorCommand(material: ColorMaterial, hex: string, from?: THREE.Color): Command {
  const before = (from ?? material.color).clone();
  const after = new THREE.Color().setStyle(hex, THREE.SRGBColorSpace);
  return {
    label: `Changed colour of "${materialName(material)}" to ${hex.toLowerCase()}`,
    apply: () => material.color.copy(after),
    revert: () => material.color.copy(before),
  };
}

export type ScalarProperty = 'roughness' | 'metalness' | 'opacity';

const SCALAR_LABEL: Record<ScalarProperty, string> = {
  roughness: 'roughness',
  metalness: 'metalness',
  opacity: 'opacity',
};

export function scalarCommand(
  material: PbrMaterial,
  property: ScalarProperty,
  value: number,
  from?: { value: number; transparent: boolean },
): Command {
  const clamped = THREE.MathUtils.clamp(value, 0, 1);
  const before = from ?? { value: material[property], transparent: material.transparent };
  // Opacity below 1 only shows with blending enabled; that switch needs a shader rebuild.
  const transparent =
    property === 'opacity' ? clamped < 1 || before.transparent : before.transparent;
  const set = (v: number, t: boolean) => {
    material[property] = v;
    if (material.transparent !== t) {
      material.transparent = t;
      material.needsUpdate = true;
    }
  };
  return {
    label: `Set ${SCALAR_LABEL[property]} of "${materialName(material)}" to ${clamped.toFixed(2)}`,
    apply: () => set(clamped, transparent),
    revert: () => set(before.value, before.transparent),
  };
}
