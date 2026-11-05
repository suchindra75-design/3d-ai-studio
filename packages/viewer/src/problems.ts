/**
 * Where the quality report's problems are: open (boundary) edges, used by one
 * triangle, and non-manifold edges, used by three or more.
 *
 * Like the backend's check, positions are welded first (texture seams and
 * normal splits duplicate vertices but are not holes) and all parts are taken
 * together. Uses numeric sorting rather than string maps, so multi-million
 * triangle models take seconds, not minutes.
 */
import * as THREE from 'three';

export interface ProblemEdges {
  /** Segment end points in world space: [x0, y0, z0, x1, y1, z1, …]. */
  boundary: Float32Array;
  nonManifold: Float32Array;
  boundaryCount: number;
  nonManifoldCount: number;
}

function collect(root: THREE.Object3D): { positions: Float64Array; triangles: Uint32Array } {
  root.updateWorldMatrix(true, true);
  const chunks: { positions: Float64Array; indices: Uint32Array }[] = [];
  let vertexTotal = 0;
  let indexTotal = 0;
  const v = new THREE.Vector3();
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (!mesh.isMesh || !mesh.visible) return;
    const position = mesh.geometry.getAttribute('position');
    if (!position) return;
    const positions = new Float64Array(position.count * 3);
    for (let i = 0; i < position.count; i += 1) {
      v.fromBufferAttribute(position, i).applyMatrix4(mesh.matrixWorld);
      positions[i * 3] = v.x;
      positions[i * 3 + 1] = v.y;
      positions[i * 3 + 2] = v.z;
    }
    const index = mesh.geometry.index;
    const count = index ? index.count : position.count;
    const indices = new Uint32Array(count - (count % 3));
    for (let k = 0; k < indices.length; k += 1) {
      indices[k] = (index ? index.getX(k) : k) + vertexTotal;
    }
    chunks.push({ positions, indices });
    vertexTotal += position.count;
    indexTotal += indices.length;
  });
  const positions = new Float64Array(vertexTotal * 3);
  const triangles = new Uint32Array(indexTotal);
  let p = 0;
  let t = 0;
  for (const chunk of chunks) {
    positions.set(chunk.positions, p);
    triangles.set(chunk.indices, t);
    p += chunk.positions.length;
    t += chunk.indices.length;
  }
  return { positions, triangles };
}

/** One id per distinct position (within a tolerance relative to the model's size). */
function weld(positions: Float64Array): Uint32Array {
  const count = positions.length / 3;
  let extent = 0;
  for (const value of positions) extent = Math.max(extent, Math.abs(value));
  const step = Math.max(extent, 1e-12) * 1e-7;
  const q = new Float64Array(positions.length);
  for (let i = 0; i < positions.length; i += 1) q[i] = Math.round(positions[i]! / step);
  const order = new Uint32Array(count);
  for (let i = 0; i < count; i += 1) order[i] = i;
  order.sort(
    (a, b) =>
      q[a * 3]! - q[b * 3]! || q[a * 3 + 1]! - q[b * 3 + 1]! || q[a * 3 + 2]! - q[b * 3 + 2]!,
  );
  const ids = new Uint32Array(count);
  let id = 0;
  for (let n = 0; n < count; n += 1) {
    const i = order[n]!;
    if (n > 0) {
      const j = order[n - 1]!;
      if (q[i * 3] !== q[j * 3] || q[i * 3 + 1] !== q[j * 3 + 1] || q[i * 3 + 2] !== q[j * 3 + 2]) {
        id += 1;
      }
    }
    ids[i] = id;
  }
  return ids;
}

export function findProblemEdges(root: THREE.Object3D): ProblemEdges {
  const { positions, triangles } = collect(root);
  const ids = weld(positions);
  const representative = new Uint32Array(ids.length);
  for (let i = ids.length - 1; i >= 0; i -= 1) representative[ids[i]!] = i;
  const base = ids.length + 1;
  // One float64 key per undirected edge: exact while base² < 2^53 (≈ 94 million vertices).
  // Written straight into one typed array (no intermediate JS array of ~9 million numbers).
  const keys = new Float64Array(triangles.length);
  let used = 0;
  for (let t = 0; t + 2 < triangles.length; t += 3) {
    for (let e = 0; e < 3; e += 1) {
      const a = ids[triangles[t + e]!]!;
      const b = ids[triangles[t + ((e + 1) % 3)]!]!;
      if (a === b) continue; // degenerate
      keys[used++] = a < b ? a * base + b : b * base + a;
    }
  }
  const sorted = keys.subarray(0, used).sort();
  const boundary: number[] = [];
  const nonManifold: number[] = [];
  const push = (target: number[], key: number) => {
    for (const vertex of [Math.floor(key / base), key % base]) {
      const i = representative[vertex]!;
      target.push(positions[i * 3]!, positions[i * 3 + 1]!, positions[i * 3 + 2]!);
    }
  };
  for (let n = 0; n < sorted.length;) {
    let m = n + 1;
    while (m < sorted.length && sorted[m] === sorted[n]) m += 1;
    const uses = m - n;
    if (uses === 1) push(boundary, sorted[n]!);
    else if (uses > 2) push(nonManifold, sorted[n]!);
    n = m;
  }
  return {
    boundary: new Float32Array(boundary),
    nonManifold: new Float32Array(nonManifold),
    boundaryCount: boundary.length / 6,
    nonManifoldCount: nonManifold.length / 6,
  };
}
