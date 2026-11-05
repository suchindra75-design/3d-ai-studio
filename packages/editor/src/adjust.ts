/**
 * Texture adjustments, baked into the data so they survive saving and every
 * export format:
 *
 * - colour (brightness, contrast, saturation, hue) is applied to the texture's
 *   pixels, or to the vertex colours of a model without a texture;
 * - tiling (repeat, offset, rotation) transforms the texture coordinates of the
 *   parts using the material, rather than a glTF extension other tools drop.
 */
import * as THREE from 'three';

import { EditError } from './arrange';
import { materialName, type PbrMaterial } from './commands';
import type { Command } from './history';

export interface ColourAdjust {
  /** -1..1: darker … brighter (multiplies). */
  brightness: number;
  /** -1..1: flatter … punchier, around mid-grey. */
  contrast: number;
  /** -1..1: grey … vivid. */
  saturation: number;
  /** Degrees, -180..180. */
  hue: number;
}

export const NO_ADJUST: ColourAdjust = { brightness: 0, contrast: 0, saturation: 0, hue: 0 };

export function isNeutral(adjust: ColourAdjust): boolean {
  return (
    adjust.brightness === 0 && adjust.contrast === 0 && adjust.saturation === 0 && adjust.hue === 0
  );
}

/** Adjusts one sRGB colour (components 0..1) in place in ``out``. */
export function adjustRgb(rgb: [number, number, number], adjust: ColourAdjust): void {
  let [r, g, b] = rgb;
  const gain = 1 + adjust.brightness;
  r *= gain;
  g *= gain;
  b *= gain;
  const contrast = 1 + adjust.contrast;
  r = (r - 0.5) * contrast + 0.5;
  g = (g - 0.5) * contrast + 0.5;
  b = (b - 0.5) * contrast + 0.5;
  const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  const saturation = 1 + adjust.saturation;
  r = luma + (r - luma) * saturation;
  g = luma + (g - luma) * saturation;
  b = luma + (b - luma) * saturation;
  if (adjust.hue !== 0) {
    // Rotation about the grey axis (keeps luminance close; exact for greys).
    const angle = (adjust.hue * Math.PI) / 180;
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    const k = (1 - cos) / 3;
    const s = Math.sqrt(1 / 3) * sin;
    const nr = r * (cos + k) + g * (k - s) + b * (k + s);
    const ng = r * (k + s) + g * (cos + k) + b * (k - s);
    const nb = r * (k - s) + g * (k + s) + b * (cos + k);
    r = nr;
    g = ng;
    b = nb;
  }
  rgb[0] = Math.min(Math.max(r, 0), 1);
  rgb[1] = Math.min(Math.max(g, 0), 1);
  rgb[2] = Math.min(Math.max(b, 0), 1);
}

/** Adjusts RGBA bytes (canvas ImageData) in place; alpha is kept. */
export function adjustPixels(data: Uint8ClampedArray, adjust: ColourAdjust): void {
  const rgb: [number, number, number] = [0, 0, 0];
  for (let i = 0; i < data.length; i += 4) {
    rgb[0] = data[i]! / 255;
    rgb[1] = data[i + 1]! / 255;
    rgb[2] = data[i + 2]! / 255;
    adjustRgb(rgb, adjust);
    data[i] = rgb[0] * 255;
    data[i + 1] = rgb[1] * 255;
    data[i + 2] = rgb[2] * 255;
  }
}

type Drawable = CanvasImageSource & { width: number; height: number };

/** The texture's pixels, adjusted, as a new canvas (at most ``maxSize`` px for previews). */
export function adjustedCanvas(
  image: Drawable,
  adjust: ColourAdjust,
  maxSize = Number.POSITIVE_INFINITY,
): HTMLCanvasElement {
  const scale = Math.min(1, maxSize / Math.max(image.width, image.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(image.width * scale));
  canvas.height = Math.max(1, Math.round(image.height * scale));
  const context = canvas.getContext('2d', { willReadFrequently: true });
  if (!context) throw new EditError('This browser cannot edit texture pixels.');
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
  adjustPixels(pixels.data, adjust);
  context.putImageData(pixels, 0, 0);
  return canvas;
}

function describe(adjust: ColourAdjust): string {
  const parts = [
    adjust.brightness
      ? `brightness ${adjust.brightness > 0 ? '+' : ''}${adjust.brightness.toFixed(2)}`
      : '',
    adjust.contrast
      ? `contrast ${adjust.contrast > 0 ? '+' : ''}${adjust.contrast.toFixed(2)}`
      : '',
    adjust.saturation
      ? `saturation ${adjust.saturation > 0 ? '+' : ''}${adjust.saturation.toFixed(2)}`
      : '',
    adjust.hue ? `hue ${adjust.hue > 0 ? '+' : ''}${Math.round(adjust.hue)}°` : '',
  ].filter(Boolean);
  return parts.join(', ');
}

/** Meshes under ``root`` that use ``material`` (by their own materials). */
export function meshesUsing(
  root: THREE.Object3D,
  material: THREE.Material,
  own: (mesh: THREE.Mesh) => THREE.Material[],
): THREE.Mesh[] {
  const found: THREE.Mesh[] = [];
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (mesh.isMesh && own(mesh).includes(material)) found.push(mesh);
  });
  return found;
}

/** What a colour adjustment changes for this material: its texture, or vertex colours. */
export function colourTarget(
  material: THREE.Material,
  meshes: THREE.Mesh[],
): 'texture' | 'vertex' | null {
  if ((material as PbrMaterial).map) return 'texture';
  if (meshes.some((m) => m.geometry.getAttribute('color'))) return 'vertex';
  return null;
}

/**
 * Bakes a colour adjustment into the material's texture (a new image; the old one
 * is kept for undo) or, without a texture, into the vertex colours of ``meshes``.
 */
export function colourAdjustCommand(
  material: PbrMaterial,
  meshes: THREE.Mesh[],
  adjust: ColourAdjust,
  /** For live previews: work on a copy of the texture at most this size. */
  previewSize?: number,
): Command {
  if (isNeutral(adjust)) throw new EditError('Nothing to change: all adjustments are at zero.');
  const name = materialName(material);
  const target = colourTarget(material, meshes);
  if (target === 'texture') {
    const before = material.map!;
    const after = before.clone();
    after.source = new THREE.Source(adjustedCanvas(before.image as Drawable, adjust, previewSize));
    after.needsUpdate = true;
    return {
      label: `Adjusted the texture of "${name}" (${describe(adjust)})`,
      apply: () => {
        material.map = after;
      },
      revert: () => {
        material.map = before;
      },
    };
  }
  if (target === 'vertex') {
    // Vertex colours are linear (glTF); adjust in sRGB, as for textures.
    const swaps = meshes
      .map((mesh) => ({ mesh, before: mesh.geometry.getAttribute('color') }))
      .filter((s): s is { mesh: THREE.Mesh; before: THREE.BufferAttribute } => Boolean(s.before))
      .map(({ mesh, before }) => {
        const after = new THREE.Float32BufferAttribute(
          before.count * before.itemSize,
          before.itemSize,
        );
        const colour = new THREE.Color();
        const rgb: [number, number, number] = [0, 0, 0];
        for (let i = 0; i < before.count; i += 1) {
          colour.setRGB(before.getX(i), before.getY(i), before.getZ(i), THREE.LinearSRGBColorSpace);
          colour.getRGB(colour, THREE.SRGBColorSpace);
          rgb[0] = colour.r;
          rgb[1] = colour.g;
          rgb[2] = colour.b;
          adjustRgb(rgb, adjust);
          colour.setRGB(rgb[0], rgb[1], rgb[2], THREE.SRGBColorSpace);
          after.setXYZ(i, colour.r, colour.g, colour.b);
          if (before.itemSize === 4) after.setW(i, before.getW(i));
        }
        return { mesh, before, after };
      });
    return {
      label: `Adjusted the colours of "${name}" (${describe(adjust)})`,
      apply: () => swaps.forEach((s) => s.mesh.geometry.setAttribute('color', s.after)),
      revert: () => swaps.forEach((s) => s.mesh.geometry.setAttribute('color', s.before)),
    };
  }
  throw new EditError('This material has no texture or vertex colours to adjust.');
}

export interface Tiling {
  repeat: [number, number];
  offset: [number, number];
  /** Degrees, about the texture's centre. */
  rotation: number;
}

export const NO_TILING: Tiling = { repeat: [1, 1], offset: [0, 0], rotation: 0 };

/** uv' = rotate(scale(uv − ½)) + ½ + offset: repeat around the centre, then shift. */
export function transformUv(u: number, v: number, tiling: Tiling): [number, number] {
  const angle = (tiling.rotation * Math.PI) / 180;
  const x = (u - 0.5) * tiling.repeat[0];
  const y = (v - 0.5) * tiling.repeat[1];
  const c = Math.cos(angle);
  const s = Math.sin(angle);
  return [x * c - y * s + 0.5 + tiling.offset[0], x * s + y * c + 0.5 + tiling.offset[1]];
}

/**
 * Bakes tiling into the texture coordinates of every part using ``material`` and
 * makes its textures repeat. Parts that share geometry with others get their own copy.
 */
export function tilingCommand(
  material: PbrMaterial,
  meshes: THREE.Mesh[],
  tiling: Tiling,
): Command {
  const { repeat, offset, rotation } = tiling;
  if (repeat[0] === 1 && repeat[1] === 1 && !offset[0] && !offset[1] && !rotation) {
    throw new EditError('Nothing to change: the tiling is 1 × 1 with no offset or rotation.');
  }
  if (!(repeat[0] > 0 && repeat[1] > 0 && repeat[0] <= 100 && repeat[1] <= 100)) {
    throw new EditError('Repeat must be more than 0 and at most 100.');
  }
  const mapped = meshes.filter((m) => m.geometry.getAttribute('uv'));
  if (mapped.length === 0) throw new EditError('These parts have no texture coordinates.');
  const swaps = mapped.map((mesh) => {
    const before = mesh.geometry;
    const after = before.clone();
    const uv = after.getAttribute('uv') as THREE.BufferAttribute;
    for (let i = 0; i < uv.count; i += 1) {
      const [u, v] = transformUv(uv.getX(i), uv.getY(i), tiling);
      uv.setXY(i, u, v);
    }
    uv.needsUpdate = true;
    return { mesh, before, after };
  });
  const textures = Object.values(material).filter(
    (value): value is THREE.Texture => value instanceof THREE.Texture,
  );
  const wraps = textures.map((t) => ({ t, s: t.wrapS, w: t.wrapT }));
  const label = `Tiled the texture of "${materialName(material)}" (${repeat[0]} × ${repeat[1]}${
    offset[0] || offset[1] ? `, offset ${offset[0]}, ${offset[1]}` : ''
  }${rotation ? `, ${rotation}°` : ''})`;
  return {
    label,
    apply: () => {
      swaps.forEach((s) => (s.mesh.geometry = s.after));
      for (const { t } of wraps) {
        t.wrapS = THREE.RepeatWrapping;
        t.wrapT = THREE.RepeatWrapping;
        t.needsUpdate = true;
      }
    },
    revert: () => {
      swaps.forEach((s) => (s.mesh.geometry = s.before));
      for (const { t, s, w } of wraps) {
        t.wrapS = s;
        t.wrapT = w;
        t.needsUpdate = true;
      }
    },
  };
}
