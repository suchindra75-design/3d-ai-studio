/**
 * Levels of detail for viewing (D58). The full model stays in the scene untouched:
 * it is what is picked, measured, edited and saved. When full detail would not be
 * visible, simplified copies are drawn instead and the full parts are moved to a
 * layer the camera does not draw (the raycaster still sees every layer).
 *
 * Each simplified part follows its full part every frame: transform, material
 * (so display modes, colour edits and the section plane apply) and visibility. A
 * part whose geometry has been edited is drawn in full from then on. The levels'
 * own materials are never drawn here, so their textures can be freed once loaded.
 */
import * as THREE from 'three';

/** Simplified versions of a loaded model, coarsest last. */
export interface LodSource {
  triangles: number;
  root: THREE.Object3D;
}

/** 'auto' picks by on-screen size; a number fixes the level (0 = full detail). */
export type DetailMode = 'auto' | number;

/** About two front-facing triangles per pixel of the model's on-screen area. */
export const TRIANGLES_PER_PIXEL = 4;
/** A level is kept until the budget differs by this much (no flicker at a boundary). */
const HYSTERESIS = 1.25;
/** Layer for full parts that are not drawn while a simplified copy is. */
export const HIDDEN_LAYER = 31;
const MAX_PARTS = 2000;

/**
 * The finest level whose triangle count fits ``area`` pixels (``triangles[0]`` is full
 * detail, coarser levels follow). Keeps ``current`` near the boundary.
 */
export function chooseLevel(triangles: number[], area: number, current: number): number {
  const budget = TRIANGLES_PER_PIXEL * Math.max(area, 0);
  let wanted = triangles.findIndex((count) => count <= budget);
  if (wanted < 0) wanted = triangles.length - 1;
  if (wanted > current && triangles[current]! <= budget * HYSTERESIS) return current;
  if (wanted < current && triangles[wanted]! > budget / HYSTERESIS) return current;
  return wanted;
}

/** Screen area (px²) covered by a sphere seen from ``camera``; Infinity when inside it. */
export function projectedArea(
  center: THREE.Vector3,
  radius: number,
  camera: THREE.Camera,
  heightPx: number,
): number {
  if (camera instanceof THREE.PerspectiveCamera) {
    const distance = camera.position.distanceTo(center);
    if (distance <= radius) return Number.POSITIVE_INFINITY;
    const halfHeight = distance * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    const r = (radius / halfHeight) * (heightPx / 2);
    return Math.PI * r * r;
  }
  return Number.POSITIVE_INFINITY;
}

function meshes(root: THREE.Object3D): THREE.Mesh[] {
  const found: THREE.Mesh[] = [];
  root.updateMatrixWorld(true);
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (mesh.isMesh) found.push(mesh);
  });
  return found;
}

// Each geometry's own box, kept while its positions are unchanged: the levels are
// set up again as each one arrives, and a fresh pass over a 10-million-triangle model
// took a third of a second each time (measured).
const localBoxes = new WeakMap<
  THREE.BufferGeometry,
  { position: unknown; version: number; box: THREE.Box3 }
>();

function localBox(geometry: THREE.BufferGeometry): THREE.Box3 {
  const position = geometry.getAttribute('position');
  const version = position && 'version' in position ? position.version : 0;
  const kept = localBoxes.get(geometry);
  if (kept && kept.position === position && kept.version === version) return kept.box;
  geometry.computeBoundingBox();
  const box = geometry.boundingBox!.clone();
  localBoxes.set(geometry, { position, version, box });
  return box;
}

function worldBox(mesh: THREE.Mesh): THREE.Box3 {
  return localBox(mesh.geometry).clone().applyMatrix4(mesh.matrixWorld);
}

/**
 * Pairs each simplified part with the full part it was made from, by world bounds
 * (simplification keeps borders, so bounds barely move). Parts without a close match
 * are left unpaired (drawn in full).
 */
export function matchParts(
  full: THREE.Mesh[],
  simplified: THREE.Mesh[],
  diagonal: number,
): [THREE.Mesh, THREE.Mesh][] {
  if (full.length > MAX_PARTS || simplified.length > MAX_PARTS) return [];
  const fullBoxes = full.map(worldBox);
  const candidates: { score: number; f: number; s: number }[] = [];
  simplified.forEach((mesh, s) => {
    const box = worldBox(mesh);
    const center = box.getCenter(new THREE.Vector3());
    const size = box.getSize(new THREE.Vector3());
    fullBoxes.forEach((other, f) => {
      const score =
        other.getCenter(new THREE.Vector3()).distanceTo(center) +
        other.getSize(new THREE.Vector3()).distanceTo(size);
      if (score <= diagonal * 0.02) candidates.push({ score, f, s });
    });
  });
  candidates.sort((a, b) => a.score - b.score);
  const usedFull = new Set<number>();
  const usedSimplified = new Set<number>();
  const pairs: [THREE.Mesh, THREE.Mesh][] = [];
  for (const { f, s } of candidates) {
    if (usedFull.has(f) || usedSimplified.has(s)) continue;
    usedFull.add(f);
    usedSimplified.add(s);
    pairs.push([full[f]!, simplified[s]!]);
  }
  return pairs;
}

const WATCHED = ['position', 'normal', 'uv', 'color'] as const;

interface Part {
  full: THREE.Mesh;
  copy: THREE.Mesh;
  /** copy's world matrix = full's world matrix × offset */
  offset: THREE.Matrix4;
  geometry: THREE.BufferGeometry;
  attributes: (THREE.BufferAttribute | THREE.InterleavedBufferAttribute | undefined)[];
  versions: number[];
  stale: boolean;
}

function snapshot(mesh: THREE.Mesh) {
  const attributes = WATCHED.map((name) => mesh.geometry.getAttribute(name));
  return {
    geometry: mesh.geometry,
    attributes,
    versions: attributes.map((a) => (a && 'version' in a ? a.version : 0)),
  };
}

function edited(part: Part): boolean {
  if (part.full.geometry !== part.geometry) return true;
  return WATCHED.some((name, i) => {
    const now = part.full.geometry.getAttribute(name);
    if (now !== part.attributes[i]) return true;
    return Boolean(now && 'version' in now && now.version !== part.versions[i]);
  });
}

/** The copy can draw with the full part's material (it has the data the material reads). */
function compatible(copy: THREE.Mesh, material: THREE.Material | THREE.Material[]): boolean {
  if (Array.isArray(material)) return false;
  const pbr = material as THREE.MeshStandardMaterial;
  if (material.vertexColors && !copy.geometry.getAttribute('color')) return false;
  if (pbr.map && !copy.geometry.getAttribute('uv')) return false;
  return true;
}

function shownIn(root: THREE.Object3D, mesh: THREE.Object3D): boolean {
  for (let object: THREE.Object3D | null = mesh; object; object = object.parent) {
    if (!object.visible) return false;
    if (object === root) return true;
  }
  return false; // removed from the model (deleted part)
}

/** Draws the model at a chosen level of detail; see the module comment. */
export class ModelLod {
  /** Add this to the scene (it holds the simplified copies, never picked). */
  readonly group = new THREE.Group();
  /** Triangle count per level, full detail first. */
  readonly triangles: number[];
  private readonly levels: Part[][];
  private current = 0;

  constructor(
    private readonly root: THREE.Object3D,
    fullTriangles: number,
    sources: LodSource[],
  ) {
    this.group.name = 'Levels of detail';
    const fullMeshes = meshes(root);
    const box = new THREE.Box3();
    for (const mesh of fullMeshes) box.union(worldBox(mesh));
    const diagonal = box.isEmpty() ? 1 : box.getSize(new THREE.Vector3()).length() || 1;
    this.triangles = [fullTriangles];
    this.levels = [];
    for (const source of sources) {
      const parts: Part[] = [];
      for (const [full, loaded] of matchParts(fullMeshes, meshes(source.root), diagonal)) {
        const offset = full.matrixWorld.clone().invert().multiply(loaded.matrixWorld);
        // A clone shares the loaded geometry; whoever loaded the levels frees them.
        const copy = new THREE.Mesh(loaded.geometry, loaded.material);
        copy.name = loaded.name;
        copy.matrixAutoUpdate = false;
        copy.matrixWorldAutoUpdate = false;
        copy.visible = false;
        copy.raycast = () => {}; // picking always uses the full parts
        this.group.add(copy);
        parts.push({ full, copy, offset, stale: false, ...snapshot(full) });
      }
      this.levels.push(parts);
      this.triangles.push(source.triangles);
    }
  }

  get level(): number {
    return this.current;
  }

  /** Shows ``level`` (0 = full detail) and brings every copy in step with its part. */
  update(level: number): void {
    this.current = Math.max(0, Math.min(level, this.levels.length));
    this.levels.forEach((parts, index) => {
      const active = index + 1 === this.current;
      for (const part of parts) {
        if (!part.stale && edited(part)) part.stale = true;
        const use = active && !part.stale && compatible(part.copy, part.full.material);
        const shown = use && shownIn(this.root, part.full);
        part.copy.visible = shown;
        if (shown) {
          part.copy.material = part.full.material;
          part.copy.castShadow = part.full.castShadow;
          part.copy.receiveShadow = part.full.receiveShadow;
          part.copy.matrixWorld.multiplyMatrices(part.full.matrixWorld, part.offset);
        }
      }
    });
    // Full parts are hidden only where a copy is drawn in their place.
    const replaced = new Set<THREE.Mesh>();
    if (this.current > 0) {
      for (const part of this.levels[this.current - 1]!) {
        if (part.copy.visible) replaced.add(part.full);
      }
    }
    this.root.traverse((object) => {
      const mesh = object as THREE.Mesh;
      if (!mesh.isMesh) return;
      if (replaced.has(mesh)) mesh.layers.set(HIDDEN_LAYER);
      else if (mesh.layers.isEnabled(HIDDEN_LAYER)) mesh.layers.set(0);
    });
  }

  /** Puts the full parts back on the drawn layer and hides the copies. Can be called
   * again (and ``update`` used again) afterwards; the loaded levels themselves are
   * freed by whoever loaded them. */
  dispose(): void {
    this.update(0);
  }
}
