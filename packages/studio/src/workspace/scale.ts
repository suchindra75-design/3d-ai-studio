import type { ModelVersionRead } from '@ai3d/shared-types';
import { formatLength, formatUnits } from '@ai3d/viewer';

export interface ScaleInfo {
  known: true;
  axis: string;
  length_mm: number;
}

/** The version's real-world scale, when the user gave a measurement (Enhance). */
export function scaleOf(version: ModelVersionRead): ScaleInfo | null {
  const scale = (version.stats as Record<string, unknown>).scale as ScaleInfo | undefined;
  return scale?.known ? scale : null;
}

/** Metres when the scale is known; the model's own units otherwise. */
export function sizeLabel(value: number, scaleKnown: boolean): string {
  return scaleKnown ? formatLength(value) : formatUnits(value);
}
