/**
 * Faster picking on large meshes (D64). three.js tests a ray against every triangle,
 * about 115 ms per million triangles on the benchmark machine: over a second per
 * click or measurement on 10 million. Here triangles are sorted along a Morton
 * (Z-order) curve into chunks with bounding boxes, kept beside the geometry (its own
 * triangle order is never changed: editing, saving and quads depend on it). A ray
 * tests the boxes, then only the triangles in the boxes it passes, nearest first,
 * and stops once no closer box remains.
 *
 * Only the nearest hit per mesh is reported (three reports every hit); clicking and
 * measuring use the nearest one. The index is built on first use and rebuilt when the
 * geometry or its positions change.
 */
import * as THREE from 'three';

/** Meshes with more triangles than this get the index. */
export const FAST_PICK_MIN_TRIANGLES = 200_000;
const CHUNK = 2048;

interface PickIndex {
  geometry: THREE.BufferGeometry;
  positionVersion: number;
  position: THREE.BufferAttribute | THREE.InterleavedBufferAttribute;
  /** Triangle numbers, sorted along the curve. */
  order: Uint32Array;
  /** Per chunk: min x, y, z, max x, y, z (local space). */
  boxes: Float32Array;
}

const indexes = new WeakMap<THREE.BufferGeometry, PickIndex>();

function triangleCount(geometry: THREE.BufferGeometry): number {
  const position = geometry.getAttribute('position');
  if (!position) return 0;
  return Math.floor((geometry.index ? geometry.index.count : position.count) / 3);
}

/** Spreads the low 10 bits of ``v`` to every third bit. */
function spread(v: number): number {
  let x = v & 0x3ff;
  x = (x | (x << 16)) & 0x030000ff;
  x = (x | (x << 8)) & 0x0300f00f;
  x = (x | (x << 4)) & 0x030c30c3;
  x = (x | (x << 2)) & 0x09249249;
  return x;
}

function vertexOf(geometry: THREE.BufferGeometry, triangle: number, corner: number): number {
  const i = triangle * 3 + corner;
  return geometry.index ? geometry.index.getX(i) : i;
}

function current(geometry: THREE.BufferGeometry): PickIndex | null {
  const position = geometry.getAttribute('position');
  const existing = indexes.get(geometry);
  if (
    existing &&
    existing.position === position &&
    existing.positionVersion === ('version' in position ? position.version : 0)
  ) {
    return existing;
  }
  return null;
}

/** Whether the index for ``geometry`` is built and up to date. */
export function isPickIndexReady(geometry: THREE.BufferGeometry): boolean {
  return current(geometry) !== null;
}

/** Builds the index in steps (it pauses every 65,536 triangles or so). */
function* buildIndex(geometry: THREE.BufferGeometry): Generator<void, PickIndex> {
  const position = geometry.getAttribute('position');
  const count = triangleCount(geometry);
  // Direct array reads (the attribute accessors cost several times more per call).
  const plain = !(position as THREE.InterleavedBufferAttribute).isInterleavedBufferAttribute;
  const coords = plain ? (position.array as ArrayLike<number>) : null;
  const indices = geometry.index ? (geometry.index.array as ArrayLike<number>) : null;
  const corner = (t: number, k: number) => (indices ? indices[t * 3 + k]! : t * 3 + k);
  const coordinate = (v: number, axis: number) =>
    coords
      ? coords[v * 3 + axis]!
      : axis === 0
        ? position.getX(v)
        : axis === 1
          ? position.getY(v)
          : position.getZ(v);
  // Bounds of the positions (in steps too: 10 million triangles take tens of ms).
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (let v = 0; v < position.count; v += 1) {
    if ((v & 0xffff) === 0) yield;
    for (let axis = 0; axis < 3; axis += 1) {
      const value = coordinate(v, axis);
      if (value < min[axis]!) min[axis] = value;
      if (value > max[axis]!) max[axis] = value;
    }
  }
  const scale = [0, 1, 2].map((axis) => {
    const extent = max[axis]! - min[axis]!;
    return extent > 0 ? 1023 / extent : 0;
  });
  // Morton code of each triangle's centre, then a two-pass 15-bit radix sort.
  const codes = new Uint32Array(count);
  let order = new Uint32Array(count);
  const [minX, minY, minZ] = [min[0]!, min[1]!, min[2]!];
  const [sx, sy, sz] = [scale[0]! / 3, scale[1]! / 3, scale[2]! / 3];
  for (let t = 0; t < count; t += 1) {
    if ((t & 0xffff) === 0) yield;
    order[t] = t;
    const a = corner(t, 0);
    const b = corner(t, 1);
    const c = corner(t, 2);
    const cx = coordinate(a, 0) + coordinate(b, 0) + coordinate(c, 0);
    const cy = coordinate(a, 1) + coordinate(b, 1) + coordinate(c, 1);
    const cz = coordinate(a, 2) + coordinate(b, 2) + coordinate(c, 2);
    const x = Math.min(1023, Math.max(0, Math.floor((cx - 3 * minX) * sx)));
    const y = Math.min(1023, Math.max(0, Math.floor((cy - 3 * minY) * sy)));
    const z = Math.min(1023, Math.max(0, Math.floor((cz - 3 * minZ) * sz)));
    codes[t] = spread(x) | (spread(y) << 1) | (spread(z) << 2);
  }
  let next = new Uint32Array(count);
  for (const shift of [0, 15]) {
    const buckets = new Uint32Array(1 << 15);
    for (let i = 0; i < count; i += 1) {
      if ((i & 0xffff) === 0) yield;
      buckets[(codes[order[i]!]! >>> shift) & 0x7fff]! += 1;
    }
    let sum = 0;
    for (let k = 0; k < buckets.length; k += 1) {
      const n = buckets[k]!;
      buckets[k] = sum;
      sum += n;
    }
    for (let i = 0; i < count; i += 1) {
      if ((i & 0xffff) === 0) yield;
      const t = order[i]!;
      next[buckets[(codes[t]! >>> shift) & 0x7fff]!++] = t;
    }
    [order, next] = [next, order];
  }
  const chunks = Math.ceil(count / CHUNK);
  const boxes = new Float32Array(chunks * 6);
  for (let chunk = 0; chunk < chunks; chunk += 1) {
    if ((chunk & 15) === 0) yield;
    let x0 = Infinity;
    let y0 = Infinity;
    let z0 = Infinity;
    let x1 = -Infinity;
    let y1 = -Infinity;
    let z1 = -Infinity;
    const end = Math.min(count, (chunk + 1) * CHUNK);
    for (let i = chunk * CHUNK; i < end; i += 1) {
      const t = order[i]!;
      for (let k = 0; k < 3; k += 1) {
        const v = corner(t, k);
        const x = coordinate(v, 0);
        const y = coordinate(v, 1);
        const z = coordinate(v, 2);
        if (x < x0) x0 = x;
        if (y < y0) y0 = y;
        if (z < z0) z0 = z;
        if (x > x1) x1 = x;
        if (y > y1) y1 = y;
        if (z > z1) z1 = z;
      }
    }
    boxes[chunk * 6] = x0;
    boxes[chunk * 6 + 1] = y0;
    boxes[chunk * 6 + 2] = z0;
    boxes[chunk * 6 + 3] = x1;
    boxes[chunk * 6 + 4] = y1;
    boxes[chunk * 6 + 5] = z1;
  }
  const built: PickIndex = {
    geometry,
    position,
    positionVersion: 'version' in position ? position.version : 0,
    order,
    boxes,
  };
  indexes.set(geometry, built);
  return built;
}

/** The index for ``geometry``: the current one, or built now. */
export function pickIndex(geometry: THREE.BufferGeometry): PickIndex {
  const ready = current(geometry);
  if (ready) return ready;
  const steps = buildIndex(geometry);
  for (;;) {
    const step = steps.next();
    if (step.done) return step.value;
  }
}

const inverse = new THREE.Matrix4();
const localRay = new THREE.Ray();
const chunkBox = new THREE.Box3();
const hitPoint = new THREE.Vector3();
const a = new THREE.Vector3();
const b = new THREE.Vector3();
const c = new THREE.Vector3();
const sphere = new THREE.Sphere();

/** ``Mesh.raycast`` using the index; nearest hit only (see the module comment). */
export function fastRaycast(
  this: THREE.Mesh,
  raycaster: THREE.Raycaster,
  intersects: THREE.Intersection[],
): void {
  const geometry = this.geometry;
  const material = this.material as THREE.Material;
  if (material === undefined) return;
  if (geometry.boundingSphere === null) geometry.computeBoundingSphere();
  sphere.copy(geometry.boundingSphere!).applyMatrix4(this.matrixWorld);
  if (!raycaster.ray.intersectsSphere(sphere)) return;
  inverse.copy(this.matrixWorld).invert();
  localRay.copy(raycaster.ray).applyMatrix4(inverse);
  const index = pickIndex(geometry);
  const { order, boxes } = index;
  const position = index.position;
  const chunks = boxes.length / 6;
  const candidates: { chunk: number; t: number }[] = [];
  for (let k = 0; k < chunks; k += 1) {
    chunkBox.min.set(boxes[k * 6]!, boxes[k * 6 + 1]!, boxes[k * 6 + 2]!);
    chunkBox.max.set(boxes[k * 6 + 3]!, boxes[k * 6 + 4]!, boxes[k * 6 + 5]!);
    const entry = localRay.intersectBox(chunkBox, hitPoint);
    if (entry) candidates.push({ chunk: k, t: entry.distanceTo(localRay.origin) });
  }
  candidates.sort((p, q) => p.t - q.t);
  const backfaceCulling = material.side === THREE.FrontSide;
  let best: { local: number; triangle: number; point: THREE.Vector3 } | null = null;
  const count = order.length;
  for (const { chunk, t } of candidates) {
    if (best && t > best.local) break; // no closer triangle can follow
    const end = Math.min(count, (chunk + 1) * CHUNK);
    for (let i = chunk * CHUNK; i < end; i += 1) {
      const triangle = order[i]!;
      const va = vertexOf(geometry, triangle, 0);
      const vb = vertexOf(geometry, triangle, 1);
      const vc = vertexOf(geometry, triangle, 2);
      a.fromBufferAttribute(position, va);
      b.fromBufferAttribute(position, vb);
      c.fromBufferAttribute(position, vc);
      const hit =
        material.side === THREE.BackSide
          ? localRay.intersectTriangle(c, b, a, true, hitPoint)
          : localRay.intersectTriangle(a, b, c, backfaceCulling, hitPoint);
      if (!hit) continue;
      const local = hit.distanceTo(localRay.origin);
      if (!best || local < best.local) best = { local, triangle, point: hit.clone() };
    }
  }
  if (!best) return;
  const point = best.point.applyMatrix4(this.matrixWorld);
  const distance = raycaster.ray.origin.distanceTo(point);
  if (distance < raycaster.near || distance > raycaster.far) return;
  const va = vertexOf(geometry, best.triangle, 0);
  const vb = vertexOf(geometry, best.triangle, 1);
  const vc = vertexOf(geometry, best.triangle, 2);
  const normal = new THREE.Vector3();
  THREE.Triangle.getNormal(
    a.fromBufferAttribute(position, va),
    b.fromBufferAttribute(position, vb),
    c.fromBufferAttribute(position, vc),
    normal,
  );
  intersects.push({
    distance,
    point,
    object: this,
    face: { a: va, b: vb, c: vc, normal, materialIndex: 0 },
    faceIndex: best.triangle,
  });
}

/**
 * Gives large single-material meshes under ``root`` the faster raycast, and builds
 * their indexes in idle time so the first click does not wait for one. Returns a
 * function that cancels building not yet started.
 */
export function acceleratePicking(root: THREE.Object3D): () => void {
  const large: THREE.Mesh[] = [];
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (!mesh.isMesh || Array.isArray(mesh.material)) return;
    if ((mesh as THREE.SkinnedMesh).isSkinnedMesh || mesh.morphTargetInfluences) return;
    if (triangleCount(mesh.geometry) < FAST_PICK_MIN_TRIANGLES) return;
    mesh.raycast = fastRaycast;
    large.push(mesh);
  });
  let cancelled = false;
  type Deadline = { timeRemaining: () => number };
  const idle =
    (globalThis as { requestIdleCallback?: (cb: (d: Deadline) => void, o?: object) => number })
      .requestIdleCallback ??
    ((cb: (d: Deadline) => void) => {
      // Where there is no idle callback: 8 ms of work every 50 ms, timed from when it runs.
      setTimeout(() => {
        const start = performance.now();
        cb({ timeRemaining: () => Math.max(0, 8 - (performance.now() - start)) });
      }, 50);
    });
  let steps: Generator<void, PickIndex> | null = null;
  let mesh: THREE.Mesh | undefined;
  // A few milliseconds at a time, so the page never stalls for the whole build.
  const work = (deadline: Deadline) => {
    if (cancelled) return;
    while (deadline.timeRemaining() > 2) {
      if (!steps) {
        mesh = large.shift();
        if (!mesh) return;
        steps = current(mesh.geometry) ? null : buildIndex(mesh.geometry);
        continue;
      }
      if (current(mesh!.geometry)) {
        steps = null; // a click built it meanwhile
        continue;
      }
      if (steps.next().done) steps = null;
    }
    idle(work, { timeout: 1000 });
  };
  if (large.length) idle(work, { timeout: 1000 });
  return () => {
    cancelled = true;
  };
}
