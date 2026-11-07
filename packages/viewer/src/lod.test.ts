import * as THREE from 'three';

import {
  HIDDEN_LAYER,
  ModelLod,
  TRIANGLES_PER_PIXEL,
  chooseLevel,
  matchParts,
  projectedArea,
} from './lod';

describe('chooseLevel', () => {
  const triangles = [4_000_000, 1_000_000, 250_000];

  it('picks the finest level that fits the screen, coarsest when none does', () => {
    expect(chooseLevel(triangles, 4_000_000 / TRIANGLES_PER_PIXEL, 0)).toBe(0);
    expect(chooseLevel(triangles, 1_000_000 / TRIANGLES_PER_PIXEL, 0)).toBe(1);
    expect(chooseLevel(triangles, 10, 0)).toBe(2);
    expect(chooseLevel(triangles, Number.POSITIVE_INFINITY, 2)).toBe(0);
  });

  it('does not flicker at a boundary', () => {
    const boundary = 1_000_000 / TRIANGLES_PER_PIXEL;
    // Just under full detail's budget: stays at full detail until well past it.
    expect(chooseLevel(triangles, (4_000_000 / TRIANGLES_PER_PIXEL) * 0.9, 0)).toBe(0);
    expect(chooseLevel(triangles, (4_000_000 / TRIANGLES_PER_PIXEL) * 0.7, 0)).toBe(1);
    // Coming back: needs clearly more room before switching to the finer level.
    expect(chooseLevel(triangles, boundary * 4 * 1.1, 1)).toBe(1);
    expect(chooseLevel(triangles, boundary * 4 * 1.3, 1)).toBe(0);
  });
});

describe('projectedArea', () => {
  it('shrinks with distance and is unbounded inside the model', () => {
    const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
    const center = new THREE.Vector3();
    camera.position.set(0, 0, 10);
    const near = projectedArea(center, 1, camera, 800);
    camera.position.set(0, 0, 20);
    const far = projectedArea(center, 1, camera, 800);
    expect(near / far).toBeCloseTo(4, 1);
    camera.position.set(0, 0, 0.5);
    expect(projectedArea(center, 1, camera, 800)).toBe(Number.POSITIVE_INFINITY);
  });
});

function part(name: string, x: number, detail: number, uv = true): THREE.Mesh {
  const geometry = new THREE.SphereGeometry(0.5, detail, detail);
  if (!uv) geometry.deleteAttribute('uv');
  const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial());
  mesh.name = name;
  mesh.position.x = x;
  return mesh;
}

function model(detail: number, uv = true): THREE.Group {
  const root = new THREE.Group();
  root.add(part('A', 0, detail, uv), part('B', 3, detail, uv));
  root.updateMatrixWorld(true);
  return root;
}

const drawn = (mesh: THREE.Object3D) => mesh.layers.isEnabled(0);

describe('matchParts', () => {
  it('pairs parts by their bounds, regardless of order', () => {
    const full = model(64);
    const simple = model(8);
    simple.children.reverse();
    simple.updateMatrixWorld(true);
    const pairs = matchParts(full.children as THREE.Mesh[], simple.children as THREE.Mesh[], 4);
    expect(pairs.map(([f, s]) => [f.name, s.name]).sort()).toEqual([
      ['A', 'A'],
      ['B', 'B'],
    ]);
  });

  it('leaves parts without a close match unpaired', () => {
    const full = model(64);
    const moved = model(8);
    moved.children[1]!.position.x = 10;
    moved.updateMatrixWorld(true);
    const pairs = matchParts(full.children as THREE.Mesh[], moved.children as THREE.Mesh[], 4);
    expect(pairs.map(([f]) => f.name)).toEqual(['A']);
  });
});

describe('ModelLod', () => {
  it('measures each part once while its positions are unchanged', () => {
    // Levels arrive one by one and are set up again each time; measuring a large
    // model again each time stalled the page.
    const root = model(64);
    const [a] = root.children as THREE.Mesh[];
    const measured = vi.spyOn(a!.geometry, 'computeBoundingBox');
    new ModelLod(root, 8000, [{ triangles: 200, root: model(8) }]);
    new ModelLod(root, 8000, [
      { triangles: 2000, root: model(16) },
      { triangles: 200, root: model(8) },
    ]);
    expect(measured).toHaveBeenCalledTimes(1);
    // An edit to the positions is measured again.
    const position = a!.geometry.getAttribute('position') as THREE.BufferAttribute;
    position.setX(0, position.getX(0) + 0.1);
    position.needsUpdate = true;
    new ModelLod(root, 8000, [{ triangles: 200, root: model(8) }]);
    expect(measured).toHaveBeenCalledTimes(2);
  });

  it('draws a level instead of the full parts, which stay pickable', () => {
    const root = model(64);
    const lod = new ModelLod(root, 8000, [{ triangles: 200, root: model(8) }]);
    const [a, b] = root.children as THREE.Mesh[];
    lod.update(1);
    expect(lod.level).toBe(1);
    expect(drawn(a!) || drawn(b!)).toBe(false);
    expect(a!.layers.isEnabled(HIDDEN_LAYER)).toBe(true);
    const copies = lod.group.children as THREE.Mesh[];
    expect(copies.every((c) => c.visible)).toBe(true);
    // The copies use the full parts' materials (display modes, colour edits, section).
    expect(copies.map((c) => c.material)).toEqual(
      expect.arrayContaining([a!.material as THREE.Material, b!.material as THREE.Material]),
    );
    // Picking goes to the full parts only.
    const raycaster = new THREE.Raycaster(new THREE.Vector3(0, 0, 5), new THREE.Vector3(0, 0, -1));
    raycaster.layers.enableAll();
    const hits = raycaster.intersectObjects([root, lod.group], true);
    expect(hits[0]!.object).toBe(a);

    lod.update(0);
    expect(drawn(a!) && drawn(b!)).toBe(true);
    expect(copies.every((c) => !c.visible)).toBe(true);
  });

  it('follows moved, hidden and deleted parts', () => {
    const root = model(64);
    const lod = new ModelLod(root, 8000, [{ triangles: 200, root: model(8) }]);
    const [a, b] = root.children as THREE.Mesh[];
    a!.position.y = 2;
    root.updateMatrixWorld(true);
    lod.update(1);
    const copyOf = (mesh: THREE.Mesh) =>
      (lod.group.children as THREE.Mesh[]).find((c) => c.name === mesh.name)!;
    expect(new THREE.Vector3().setFromMatrixPosition(copyOf(a!).matrixWorld).y).toBeCloseTo(2);

    b!.visible = false; // hidden in the hierarchy
    lod.update(1);
    expect(copyOf(b!).visible).toBe(false);
    root.remove(a!); // deleted
    lod.update(1);
    expect(copyOf(a!).visible).toBe(false);
  });

  it('draws an edited part in full from then on', () => {
    const root = model(64);
    const lod = new ModelLod(root, 8000, [{ triangles: 200, root: model(8) }]);
    const [a, b] = root.children as THREE.Mesh[];
    a!.geometry.getAttribute('position').needsUpdate = true; // e.g. vertices changed
    lod.update(1);
    expect(drawn(a!)).toBe(true);
    expect(drawn(b!)).toBe(false);
    b!.geometry = b!.geometry.clone(); // e.g. tiling replaces the geometry
    lod.update(1);
    expect(drawn(b!)).toBe(true);
    lod.update(0);
    lod.update(1);
    expect(drawn(a!) && drawn(b!)).toBe(true); // stays full
  });

  it('keeps full detail where the copy lacks what the material needs', () => {
    const root = model(64);
    (root.children[0] as THREE.Mesh).material = new THREE.MeshStandardMaterial({
      map: new THREE.Texture(),
    });
    const lod = new ModelLod(root, 8000, [{ triangles: 200, root: model(8, false) }]);
    lod.update(1);
    expect(drawn(root.children[0]!)).toBe(true); // textured: the copy has no UVs
    expect(drawn(root.children[1]!)).toBe(false);
  });

  it('can be undone and used again (React effects may run twice)', () => {
    const root = model(64);
    const lod = new ModelLod(root, 8000, [{ triangles: 200, root: model(8) }]);
    lod.update(1);
    lod.dispose();
    expect(root.children.every(drawn)).toBe(true);
    lod.update(1);
    expect(root.children.some(drawn)).toBe(false);
  });
});
