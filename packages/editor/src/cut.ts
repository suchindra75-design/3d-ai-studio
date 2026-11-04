/**
 * Cutting the model with a plane.
 *
 * Every triangle is clipped against the plane; new vertices on cut edges get
 * every attribute (normals, texture coordinates, colours…) interpolated, and
 * are shared between neighbouring triangles, so a closed surface stays
 * connected. Where the cut leaves closed outlines, they are filled with a flat
 * cap (three.js' Earcut, holes included) so a watertight model stays
 * watertight. The cap takes the colours of the cut edge.
 */
import { displayName } from '@ai3d/viewer';
import * as THREE from 'three';

import { EditError } from './arrange';
import type { Command } from './history';

export interface CutSummary {
  /** Parts whose geometry was cut. */
  cut: number;
  /** Parts that were entirely on the removed side. */
  removed: number;
  /** Closed outlines filled with a cap. */
  capped: number;
  /** Cut outlines left open because the surface is not closed there. */
  open: number;
  /** Parts that cannot be cut here (skinned, instanced or with shape keys). */
  skipped: string[];
}

export interface CutResult {
  command: Command;
  summary: CutSummary;
}

type CutGeometry = { geometry: THREE.BufferGeometry; capped: number; open: number };

/** Signed distance per vertex, snapped to 0 within a tolerance relative to the part's size. */
function distances(
  position: THREE.BufferAttribute | THREE.InterleavedBufferAttribute,
  plane: THREE.Plane,
) {
  const d = new Float64Array(position.count);
  const v = new THREE.Vector3();
  let extent = 0;
  for (let i = 0; i < position.count; i += 1) {
    v.fromBufferAttribute(position, i);
    d[i] = plane.distanceToPoint(v);
    extent = Math.max(extent, Math.abs(v.x), Math.abs(v.y), Math.abs(v.z));
  }
  const tolerance = Math.max(extent, 1e-12) * 1e-7;
  for (let i = 0; i < d.length; i += 1) if (Math.abs(d[i]!) <= tolerance) d[i] = 0;
  return { d, tolerance };
}

interface Builder {
  names: string[];
  sizes: number[];
  sources: (THREE.BufferAttribute | THREE.InterleavedBufferAttribute)[];
  data: number[][];
  onPlane: boolean[];
  count: number;
  /** Positions closer than this are the same point (welding). */
  tolerance: number;
}

function addOriginal(b: Builder, index: number, onPlane: boolean): number {
  b.sources.forEach((source, a) => {
    for (let c = 0; c < b.sizes[a]!; c += 1) b.data[a]!.push(source.getComponent(index, c));
  });
  b.onPlane.push(onPlane);
  return b.count++;
}

function addBetween(b: Builder, i: number, j: number, t: number): number {
  b.sources.forEach((source, a) => {
    const size = b.sizes[a]!;
    const values: number[] = [];
    for (let c = 0; c < size; c += 1) {
      const x = source.getComponent(i, c);
      values.push(x + (source.getComponent(j, c) - x) * t);
    }
    if (b.names[a] === 'normal' || (b.names[a] === 'tangent' && size === 4)) {
      const length = Math.hypot(values[0]!, values[1]!, values[2]!) || 1;
      for (let c = 0; c < 3; c += 1) values[c]! /= length;
      if (size === 4) values[3] = source.getComponent(i, 3);
    }
    b.data[a]!.push(...values);
  });
  b.onPlane.push(true);
  return b.count++;
}

/** Fills the closed outlines of the cut; returns how many were filled and left open. */
function capOutlines(
  b: Builder,
  triangles: number[],
  plane: THREE.Plane,
): { faces: number[]; capped: number; open: number } {
  const positionAt = b.names.indexOf('position');
  const pos = b.data[positionAt]!;
  // Weld by position so texture seams do not look like boundaries.
  const weld = new Map<string, number>();
  const weldOf: number[] = [];
  const representative: number[] = [];
  for (let v = 0; v < b.count; v += 1) {
    const q = (x: number) => Math.round(x / b.tolerance);
    const key = `${q(pos[v * 3]!)},${q(pos[v * 3 + 1]!)},${q(pos[v * 3 + 2]!)}`;
    let id = weld.get(key);
    if (id === undefined) {
      id = representative.length;
      weld.set(key, id);
      representative.push(v);
    }
    weldOf.push(id);
  }
  // Directed edges on the plane; a boundary edge has no reverse twin.
  const edges = new Map<string, [number, number]>();
  for (let f = 0; f < triangles.length; f += 3) {
    for (let e = 0; e < 3; e += 1) {
      const a = triangles[f + e]!;
      const c = triangles[f + ((e + 1) % 3)]!;
      if (!b.onPlane[a] || !b.onPlane[c]) continue;
      const wa = weldOf[a]!;
      const wc = weldOf[c]!;
      if (wa === wc) continue;
      const reverse = `${wc}>${wa}`;
      if (edges.has(reverse)) edges.delete(reverse);
      else edges.set(`${wa}>${wc}`, [wa, wc]);
    }
  }
  const next = new Map<number, number>();
  let branching = false;
  for (const [a, c] of edges.values()) {
    if (next.has(a)) branching = true;
    next.set(a, c);
  }
  const loops: number[][] = [];
  let open = 0;
  const seen = new Set<number>();
  for (const start of next.keys()) {
    if (seen.has(start)) continue;
    const loop: number[] = [];
    let current: number | undefined = start;
    while (current !== undefined && !seen.has(current)) {
      seen.add(current);
      loop.push(current);
      current = next.get(current);
    }
    if (current === start && loop.length >= 3 && !branching) loops.push(loop);
    else open += 1;
  }
  if (loops.length === 0) return { faces: [], capped: 0, open };

  // 2D coordinates in the plane.
  const u = new THREE.Vector3();
  const w = new THREE.Vector3();
  const normal = plane.normal.clone().normalize();
  u.copy(Math.abs(normal.x) < 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0))
    .cross(normal)
    .normalize();
  w.crossVectors(normal, u);
  const flat = (id: number) => {
    const v = representative[id]!;
    const p = new THREE.Vector3(pos[v * 3]!, pos[v * 3 + 1]!, pos[v * 3 + 2]!);
    return new THREE.Vector2(p.dot(u), p.dot(w));
  };
  const outlines = loops.map((loop) => loop.map(flat));
  const inside = (point: THREE.Vector2, polygon: THREE.Vector2[]) => {
    let hit = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
      const a = polygon[i]!;
      const c = polygon[j]!;
      if (
        a.y > point.y !== c.y > point.y &&
        point.x < ((c.x - a.x) * (point.y - a.y)) / (c.y - a.y) + a.x
      ) {
        hit = !hit;
      }
    }
    return hit;
  };
  // Nesting: an outline inside an odd number of others is a hole.
  const parents = outlines.map((outline, i) =>
    outlines
      .map((other, j) => (i !== j && inside(outline[0]!, other) ? j : -1))
      .filter((j) => j >= 0),
  );
  const faces: number[] = [];
  // Cap vertices: copies of the outline vertices with the cap's own normal.
  const capVertex = new Map<number, number>();
  const vertexFor = (id: number, capNormal: THREE.Vector3) => {
    let v = capVertex.get(id);
    if (v === undefined) {
      const source = representative[id]!;
      b.names.forEach((name, a) => {
        const size = b.sizes[a]!;
        const values = b.data[a]!.slice(source * size, source * size + size);
        if (name === 'normal') values.splice(0, 3, capNormal.x, capNormal.y, capNormal.z);
        b.data[a]!.push(...values);
      });
      b.onPlane.push(true);
      v = b.count++;
      capVertex.set(id, v);
    }
    return v;
  };
  outlines.forEach((outline, i) => {
    if (parents[i]!.length % 2 === 1) return; // a hole; filled with its outer outline
    const holes = outlines
      .map((_, j) => j)
      .filter((j) => parents[j]!.length === parents[i]!.length + 1 && parents[j]!.includes(i));
    const ids = [loops[i]!, ...holes.map((j) => loops[j]!)].flat();
    // The cap must run against the outline's edges, so it agrees with the
    // neighbouring triangles (also on a model whose faces point inwards).
    // A counter-clockwise outline in (u, w) needs clockwise cap triangles,
    // which face -normal; and the other way round.
    const capCcw = THREE.ShapeUtils.area(outline) < 0;
    const capNormal = capCcw ? normal.clone() : normal.clone().negate();
    const triangulated = THREE.ShapeUtils.triangulateShape(
      outline.map((p) => p.clone()),
      holes.map((j) => outlines[j]!.map((p) => p.clone())),
    );
    for (const [a, c, d] of triangulated) {
      const pa = flat(ids[a!]!);
      const pc = flat(ids[c!]!);
      const pd = flat(ids[d!]!);
      const ccw = (pc.x - pa.x) * (pd.y - pa.y) - (pc.y - pa.y) * (pd.x - pa.x) > 0;
      const tri = [ids[a!]!, ids[c!]!, ids[d!]!].map((id) => vertexFor(id, capNormal));
      faces.push(...(ccw === capCcw ? tri : [tri[0]!, tri[2]!, tri[1]!]));
    }
  });
  return { faces, capped: outlines.filter((_, i) => parents[i]!.length % 2 === 0).length, open };
}

/**
 * Keeps the part of ``geometry`` on the side of ``plane`` its normal points to.
 * Returns null when nothing is left, or the same geometry when nothing is cut.
 */
export function cutGeometry(
  geometry: THREE.BufferGeometry,
  plane: THREE.Plane,
  cap = true,
): CutGeometry | null {
  const position = geometry.getAttribute('position');
  const { d, tolerance } = distances(position, plane);
  if (d.every((value) => value >= 0)) return { geometry, capped: 0, open: 0 };
  if (d.every((value) => value <= 0)) return null;

  const names = Object.keys(geometry.attributes);
  const b: Builder = {
    names,
    sizes: names.map((name) => geometry.getAttribute(name).itemSize),
    sources: names.map((name) => geometry.getAttribute(name)),
    data: names.map(() => []),
    onPlane: [],
    count: 0,
    tolerance,
  };
  const kept = new Map<number, number>();
  const between = new Map<string, number>();
  const original = (i: number) => {
    let v = kept.get(i);
    if (v === undefined) {
      v = addOriginal(b, i, d[i] === 0);
      kept.set(i, v);
    }
    return v;
  };
  const crossing = (i: number, j: number) => {
    const key = i < j ? `${i}:${j}` : `${j}:${i}`;
    let v = between.get(key);
    if (v === undefined) {
      const [a, c] = i < j ? [i, j] : [j, i];
      v = addBetween(b, a, c, d[a]! / (d[a]! - d[c]!));
      between.set(key, v);
    }
    return v;
  };

  const index = geometry.index;
  const count = index ? index.count : position.count;
  const at = (k: number) => (index ? index.getX(k) : k);
  const groups =
    geometry.groups.length > 0 ? geometry.groups : [{ start: 0, count, materialIndex: 0 }];
  const triangles: number[] = [];
  const newGroups: { start: number; count: number; materialIndex: number }[] = [];
  const cutGroups = new Map<number, number>();
  for (const group of groups) {
    const start = triangles.length;
    const end = Math.min(group.start + group.count, count);
    for (let k = group.start; k + 2 < end; k += 3) {
      const corners = [at(k), at(k + 1), at(k + 2)];
      const signs = corners.map((i) => Math.sign(d[i]!));
      if (signs.every((s) => s >= 0)) {
        if (signs.every((s) => s === 0)) continue; // lies in the plane; the cap replaces it
        triangles.push(...corners.map(original));
        continue;
      }
      if (signs.every((s) => s <= 0)) continue;
      const polygon: number[] = [];
      for (let e = 0; e < 3; e += 1) {
        const i = corners[e]!;
        const j = corners[(e + 1) % 3]!;
        if (d[i]! >= 0) polygon.push(original(i));
        if ((d[i]! > 0 && d[j]! < 0) || (d[i]! < 0 && d[j]! > 0)) polygon.push(crossing(i, j));
      }
      for (let p = 1; p + 1 < polygon.length; p += 1) {
        triangles.push(polygon[0]!, polygon[p]!, polygon[p + 1]!);
      }
      const material = group.materialIndex ?? 0;
      cutGroups.set(material, (cutGroups.get(material) ?? 0) + 1);
    }
    if (triangles.length > start) {
      newGroups.push({
        start,
        count: triangles.length - start,
        materialIndex: group.materialIndex ?? 0,
      });
    }
  }
  if (triangles.length === 0) return null;

  let capped = 0;
  let open = 0;
  if (cap) {
    const result = capOutlines(b, triangles, plane);
    capped = result.capped;
    open = result.open;
    if (result.faces.length > 0) {
      const material = [...cutGroups.entries()].sort((x, y) => y[1] - x[1])[0]?.[0] ?? 0;
      const start = triangles.length;
      triangles.push(...result.faces);
      newGroups.push({ start, count: result.faces.length, materialIndex: material });
    }
  }

  const out = new THREE.BufferGeometry();
  out.name = geometry.name;
  out.userData = { ...geometry.userData };
  names.forEach((name, a) => {
    out.setAttribute(name, new THREE.Float32BufferAttribute(b.data[a]!, b.sizes[a]!));
  });
  out.setIndex(
    b.count > 65535
      ? new THREE.Uint32BufferAttribute(triangles, 1)
      : new THREE.Uint16BufferAttribute(triangles, 1),
  );
  if (geometry.groups.length > 0) {
    for (const group of newGroups) out.addGroup(group.start, group.count, group.materialIndex);
  }
  out.computeBoundingBox();
  out.computeBoundingSphere();
  return { geometry: out, capped, open };
}

/**
 * Cuts every part of the model with a world-space plane, keeping the side its
 * normal points to. Parts entirely on the other side are removed.
 */
export function cutCommand(
  root: THREE.Object3D,
  plane: THREE.Plane,
  label: string,
  cap = true,
): CutResult {
  root.updateWorldMatrix(true, true);
  const summary: CutSummary = { cut: 0, removed: 0, capped: 0, open: 0, skipped: [] };
  const swaps: { mesh: THREE.Mesh; before: THREE.BufferGeometry; after: THREE.BufferGeometry }[] =
    [];
  const removals: { object: THREE.Object3D; parent: THREE.Object3D; index: number }[] = [];
  const meshes: THREE.Mesh[] = [];
  root.traverse((object) => {
    if ((object as THREE.Mesh).isMesh) meshes.push(object as THREE.Mesh);
  });
  for (const mesh of meshes) {
    if (
      (mesh as THREE.SkinnedMesh).isSkinnedMesh ||
      (mesh as THREE.InstancedMesh).isInstancedMesh ||
      Object.keys(mesh.geometry.morphAttributes).length > 0
    ) {
      summary.skipped.push(displayName(mesh));
      continue;
    }
    const local = plane.clone().applyMatrix4(mesh.matrixWorld.clone().invert());
    const result = cutGeometry(mesh.geometry, local, cap);
    if (result === null) {
      if (
        mesh.children.some((child) => child.children.length > 0 || (child as THREE.Mesh).isMesh)
      ) {
        // Keep the node (its children are cut separately); drop only its triangles.
        const empty = new THREE.BufferGeometry();
        for (const name of Object.keys(mesh.geometry.attributes)) {
          const source = mesh.geometry.getAttribute(name);
          empty.setAttribute(name, new THREE.Float32BufferAttribute([], source.itemSize));
        }
        swaps.push({ mesh, before: mesh.geometry, after: empty });
      } else {
        removals.push({
          object: mesh,
          parent: mesh.parent!,
          index: mesh.parent!.children.indexOf(mesh),
        });
      }
      summary.removed += 1;
    } else if (result.geometry !== mesh.geometry) {
      swaps.push({ mesh, before: mesh.geometry, after: result.geometry });
      summary.cut += 1;
      summary.capped += result.capped;
      summary.open += result.open;
    }
  }
  if (swaps.length === 0 && removals.length === 0) {
    throw new EditError('The plane does not cut the model: everything is on the kept side.');
  }
  if (summary.removed === meshes.length - summary.skipped.length && summary.cut === 0) {
    throw new EditError('Nothing would be left: the whole model is on the removed side.');
  }
  // Remove later siblings first so the stored indices stay valid on undo.
  removals.sort((x, y) => y.index - x.index);
  return {
    summary,
    command: {
      label,
      apply: () => {
        for (const swap of swaps) swap.mesh.geometry = swap.after;
        for (const removal of removals) removal.parent.remove(removal.object);
      },
      revert: () => {
        for (const swap of swaps) swap.mesh.geometry = swap.before;
        for (const removal of [...removals].reverse()) {
          removal.parent.add(removal.object);
          removal.parent.children.pop();
          removal.parent.children.splice(removal.index, 0, removal.object);
        }
      },
    },
  };
}
