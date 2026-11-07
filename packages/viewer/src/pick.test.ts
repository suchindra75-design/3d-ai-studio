import * as THREE from 'three';

import {
  FAST_PICK_MIN_TRIANGLES,
  acceleratePicking,
  fastRaycast,
  isPickIndexReady,
  pickIndex,
} from './pick';

function bumpySphere(detail: number, indexed: boolean, side: THREE.Side): THREE.Mesh {
  let geometry: THREE.BufferGeometry = new THREE.SphereGeometry(1, detail, detail);
  const position = geometry.getAttribute('position');
  for (let i = 0; i < position.count; i += 1) {
    const s = 1 + 0.05 * Math.sin(i * 12.9898) * Math.cos(i * 78.233);
    position.setXYZ(i, position.getX(i) * s, position.getY(i) * s, position.getZ(i) * s);
  }
  if (!indexed) geometry = geometry.toNonIndexed();
  const mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({ side }));
  mesh.position.set(0.3, -0.2, 0.1);
  mesh.rotation.set(0.4, 1.1, -0.3);
  mesh.scale.set(1.5, 0.8, 1.2);
  mesh.updateMatrixWorld(true);
  return mesh;
}

/** Rays from around the mesh towards points near its centre (some miss). */
function rays(count: number): THREE.Raycaster[] {
  const result: THREE.Raycaster[] = [];
  for (let i = 0; i < count; i += 1) {
    const angle = i * 2.399963;
    const height = ((i % 17) / 16) * 2 - 1;
    const origin = new THREE.Vector3(Math.cos(angle) * 5, height * 4, Math.sin(angle) * 5);
    const target = new THREE.Vector3(((i % 7) - 3) * 0.4, ((i % 5) - 2) * 0.4, ((i % 3) - 1) * 0.4);
    result.push(new THREE.Raycaster(origin, target.sub(origin).normalize()));
  }
  return result;
}

function nearest(mesh: THREE.Mesh, raycaster: THREE.Raycaster, fast: boolean) {
  const hits: THREE.Intersection[] = [];
  if (fast) fastRaycast.call(mesh, raycaster, hits);
  else THREE.Mesh.prototype.raycast.call(mesh, raycaster, hits);
  hits.sort((p, q) => p.distance - q.distance);
  return hits[0] ?? null;
}

describe('fast picking', () => {
  for (const indexed of [true, false]) {
    for (const side of [THREE.FrontSide, THREE.DoubleSide]) {
      it(`finds the same nearest hit as three.js (${indexed ? 'indexed' : 'not indexed'}, ${side === THREE.FrontSide ? 'front' : 'double'}-sided)`, () => {
        const mesh = bumpySphere(96, indexed, side);
        let compared = 0;
        let missed = 0;
        for (const raycaster of rays(200)) {
          const expected = nearest(mesh, raycaster, false);
          const got = nearest(mesh, raycaster, true);
          if (!expected) {
            expect(got).toBeNull();
            missed += 1;
            continue;
          }
          expect(got).not.toBeNull();
          expect(got!.faceIndex).toBe(expected.faceIndex);
          expect(got!.distance).toBeCloseTo(expected.distance, 6);
          expect(got!.point.distanceTo(expected.point)).toBeLessThan(1e-6);
          compared += 1;
        }
        expect(compared).toBeGreaterThan(100);
        expect(missed).toBeGreaterThan(0);
      });
    }
  }

  it('rebuilds its index after the positions change (an edit)', () => {
    const mesh = bumpySphere(64, true, THREE.FrontSide);
    const first = pickIndex(mesh.geometry);
    expect(pickIndex(mesh.geometry)).toBe(first);
    const position = mesh.geometry.getAttribute('position') as THREE.BufferAttribute;
    for (let i = 0; i < position.count; i += 1) position.setX(i, position.getX(i) + 3);
    position.needsUpdate = true;
    mesh.geometry.boundingSphere = null;
    expect(pickIndex(mesh.geometry)).not.toBe(first);
    for (const raycaster of rays(40)) {
      const expected = nearest(mesh, raycaster, false);
      const got = nearest(mesh, raycaster, true);
      expect(got?.faceIndex ?? null).toBe(expected?.faceIndex ?? null);
    }
  });

  it('builds the index ahead of time, in small steps, so a click finds it ready', async () => {
    const segments = Math.ceil(Math.sqrt(FAST_PICK_MIN_TRIANGLES / 2)) + 1;
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(1, 1, segments, segments),
      new THREE.MeshBasicMaterial(),
    );
    const root = new THREE.Group();
    root.add(mesh);
    acceleratePicking(root);
    const start = Date.now();
    let built = false;
    while (!built && Date.now() - start < 10_000) {
      await new Promise((resolve) => setTimeout(resolve, 20));
      built = isPickIndexReady(mesh.geometry);
    }
    expect(built).toBe(true);
    const ready = pickIndex(mesh.geometry);
    expect(pickIndex(mesh.geometry)).toBe(ready); // reused, not rebuilt
  });

  it('pauses often while building, so no step blocks the page for long', async () => {
    // A budget that allows two steps per idle period: the number of periods shows how
    // finely the build is split (every phase pauses about every 65,536 items).
    const segments = 1000; // 2 million triangles
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(1, 1, segments, segments),
      new THREE.MeshBasicMaterial(),
    );
    const root = new THREE.Group();
    root.add(mesh);
    let periods = 0;
    const scope = globalThis as unknown as { requestIdleCallback?: unknown };
    const saved = scope.requestIdleCallback;
    scope.requestIdleCallback = (callback: (d: { timeRemaining: () => number }) => void) =>
      setTimeout(() => {
        periods += 1;
        let calls = 0;
        callback({ timeRemaining: () => ((calls += 1) <= 2 ? 3 : 0) });
      }, 0);
    try {
      acceleratePicking(root);
      while (!isPickIndexReady(mesh.geometry)) {
        await new Promise((resolve) => setTimeout(resolve, 5));
      }
    } finally {
      scope.requestIdleCallback = saved;
    }
    const triangles = segments * segments * 2;
    // Bounds, codes, two sort passes of two loops each, boxes: at least four times
    // the triangles / 65,536 steps (the sort alone once paused every million).
    expect(periods * 2).toBeGreaterThan((4 * triangles) / 65_536);
  });

  it('is used only for large single-material meshes', () => {
    const root = new THREE.Group();
    const small = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshBasicMaterial());
    const segments = Math.ceil(Math.sqrt(FAST_PICK_MIN_TRIANGLES / 2)) + 1;
    const large = new THREE.Mesh(
      new THREE.PlaneGeometry(1, 1, segments, segments),
      new THREE.MeshBasicMaterial(),
    );
    root.add(small, large);
    acceleratePicking(root);
    expect(small.raycast).toBe(THREE.Mesh.prototype.raycast);
    expect(large.raycast).toBe(fastRaycast);
  });
});
