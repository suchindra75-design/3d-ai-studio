import { measure, parseGlb } from '@ai3d/viewer';
import * as THREE from 'three';

import {
  EditError,
  centreOnOriginCommand,
  deleteCommand,
  mirrorCommand,
  pivotToCentreCommand,
  placeOnGroundCommand,
} from './arrange';
import { cutCommand, cutGeometry } from './cut';
import { exportGlb } from './export';
import { EditHistory } from './history';
import { MAX_TEXTURE_SIZE, canTexture, textureCommand, uvTriangles } from './texture';

function scene(...meshes: THREE.Object3D[]): THREE.Group {
  const root = new THREE.Group();
  root.add(...meshes);
  root.updateMatrixWorld(true);
  return root;
}

function mesh(geometry: THREE.BufferGeometry, name = 'Part'): THREE.Mesh {
  const result = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ name: 'Paint' }));
  result.name = name;
  return result;
}

/** Triangles as world-space corner triples. */
function triangles(object: THREE.Object3D): THREE.Vector3[][] {
  const out: THREE.Vector3[][] = [];
  object.updateWorldMatrix(true, true);
  object.traverse((child) => {
    const m = child as THREE.Mesh;
    if (!m.isMesh) return;
    const position = m.geometry.getAttribute('position');
    const index = m.geometry.index;
    const count = index ? index.count : position.count;
    for (let k = 0; k + 2 < count; k += 3) {
      out.push(
        [0, 1, 2].map((c) =>
          new THREE.Vector3()
            .fromBufferAttribute(position, index ? index.getX(k + c) : k + c)
            .applyMatrix4(m.matrixWorld),
        ),
      );
    }
  });
  return out;
}

/** Signed volume: positive when the faces point outwards. */
function volume(object: THREE.Object3D): number {
  return triangles(object).reduce(
    (sum, [a, b, c]) => sum + a!.dot(new THREE.Vector3().crossVectors(b!, c!)) / 6,
    0,
  );
}

/** Every edge (positions welded) is used by exactly two triangles, in opposite directions. */
function watertight(object: THREE.Object3D): boolean {
  const key = (v: THREE.Vector3) =>
    v
      .toArray()
      .map((x) => Math.round(x * 1e6) + 0) // + 0 turns -0 into 0
      .join(',');
  const edges = new Map<string, number>();
  for (const tri of triangles(object)) {
    for (let e = 0; e < 3; e += 1) {
      const a = key(tri[e]!);
      const b = key(tri[(e + 1) % 3]!);
      if (a === b) continue;
      edges.set(`${a}>${b}`, (edges.get(`${a}>${b}`) ?? 0) + 1);
    }
  }
  for (const [edge, count] of edges) {
    const [a, b] = edge.split('>');
    if (count !== 1 || edges.get(`${b}>${a}`) !== 1) return false;
  }
  return true;
}

describe('cutting with a plane', () => {
  it('keeps the upper half of a box, closed, with the right volume, and undoes', () => {
    const box = mesh(new THREE.BoxGeometry(1, 1, 1));
    const root = scene(box);
    const history = new EditHistory();
    const { command, summary } = cutCommand(
      root,
      new THREE.Plane(new THREE.Vector3(0, 1, 0), 0),
      'Cut',
    );
    history.execute(command);
    expect(summary).toMatchObject({ cut: 1, removed: 0, capped: 1, open: 0 });
    const bounds = measure(root);
    expect(bounds.min.y).toBeCloseTo(0, 6);
    expect(bounds.max.y).toBeCloseTo(0.5, 6);
    expect(volume(root)).toBeCloseTo(0.5, 6);
    expect(watertight(root)).toBe(true);
    // The cap faces down, away from the kept side.
    const normals = box.geometry.getAttribute('normal');
    const down = Array.from({ length: normals.count }, (_, i) => normals.getY(i)).filter(
      (y) => y === -1,
    );
    expect(down.length).toBeGreaterThan(0);
    history.undo();
    expect(volume(root)).toBeCloseTo(1, 6);
  });

  it('winds the cap like its neighbours, also on a model whose faces point inwards', () => {
    const geometry = new THREE.BoxGeometry(1, 1, 1);
    const index = geometry.index!;
    for (let k = 0; k < index.count; k += 3) {
      const b = index.getX(k + 1);
      index.setX(k + 1, index.getX(k + 2));
      index.setX(k + 2, b);
    }
    const root = scene(mesh(geometry));
    expect(volume(root)).toBeCloseTo(-1, 6); // inside out
    cutCommand(root, new THREE.Plane(new THREE.Vector3(0, 1, 0), 0), 'Cut').command.apply();
    expect(watertight(root)).toBe(true); // every edge runs both ways: consistent winding
    expect(volume(root)).toBeCloseTo(-0.5, 6);
  });

  it('fills a ring-shaped cut with a hole (torus) and keeps it closed', () => {
    const torus = mesh(new THREE.TorusGeometry(1, 0.25, 24, 48));
    const root = scene(torus);
    const full = volume(root);
    const { command, summary } = cutCommand(
      root,
      new THREE.Plane(new THREE.Vector3(0, 0, 1), 0),
      'Cut',
    );
    command.apply();
    expect(summary.capped).toBe(1);
    expect(watertight(root)).toBe(true);
    expect(volume(root)).toBeCloseTo(full / 2, 4);
  });

  it('interpolates vertex colours and texture coordinates on cut edges', () => {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute(
      'position',
      new THREE.Float32BufferAttribute([0, -1, 0, 1, 1, 0, -1, 1, 0], 3),
    );
    geometry.setAttribute(
      'color',
      new THREE.Float32BufferAttribute([0, 0, 0, 1, 1, 1, 1, 1, 1], 3),
    );
    geometry.setAttribute('uv', new THREE.Float32BufferAttribute([0.5, 0, 1, 1, 0, 1], 2));
    const result = cutGeometry(geometry, new THREE.Plane(new THREE.Vector3(0, 1, 0), 0));
    expect(result).not.toBeNull();
    const out = result!.geometry;
    const position = out.getAttribute('position');
    const colour = out.getAttribute('color');
    const uv = out.getAttribute('uv');
    const onPlane = Array.from({ length: position.count }, (_, i) => i).filter(
      (i) => Math.abs(position.getY(i)) < 1e-9,
    );
    expect(onPlane).toHaveLength(2);
    for (const i of onPlane) {
      expect(colour.getX(i)).toBeCloseTo(0.5, 6);
      expect(uv.getY(i)).toBeCloseTo(0.5, 6);
    }
    // An open surface leaves its cut open: nothing to fill.
    expect(result!.open).toBe(1);
    expect(result!.capped).toBe(0);
  });

  it('removes parts on the other side, keeps parts on the kept side, and puts them back', () => {
    const below = mesh(new THREE.BoxGeometry(1, 1, 1), 'Below');
    below.position.y = -2;
    const across = mesh(new THREE.BoxGeometry(1, 1, 1), 'Across');
    const above = mesh(new THREE.BoxGeometry(1, 1, 1), 'Above');
    above.position.y = 2;
    const root = scene(below, across, above);
    const history = new EditHistory();
    const { command, summary } = cutCommand(
      root,
      new THREE.Plane(new THREE.Vector3(0, 1, 0), 0),
      'Cut',
    );
    history.execute(command);
    expect(summary).toMatchObject({ cut: 1, removed: 1 });
    expect(root.children.map((c) => c.name)).toEqual(['Across', 'Above']);
    history.undo();
    expect(root.children.map((c) => c.name)).toEqual(['Below', 'Across', 'Above']);
  });

  it('cuts parts in their own coordinates (rotated and scaled parts)', () => {
    const box = mesh(new THREE.BoxGeometry(1, 1, 1));
    box.rotation.z = Math.PI / 2;
    box.scale.set(2, 1, 1); // 2 tall after the rotation
    const root = scene(box);
    cutCommand(root, new THREE.Plane(new THREE.Vector3(0, -1, 0), 0.5), 'Cut').command.apply();
    const bounds = measure(root);
    expect(bounds.min.y).toBeCloseTo(-1, 6);
    expect(bounds.max.y).toBeCloseTo(0.5, 6);
    expect(volume(root)).toBeCloseTo(1.5, 6);
    expect(watertight(root)).toBe(true);
  });

  it('refuses a plane that misses the model or removes all of it', () => {
    const root = scene(mesh(new THREE.BoxGeometry(1, 1, 1)));
    expect(() => cutCommand(root, new THREE.Plane(new THREE.Vector3(0, 1, 0), 5), 'Cut')).toThrow(
      /does not cut/,
    );
    expect(() => cutCommand(root, new THREE.Plane(new THREE.Vector3(0, 1, 0), -5), 'Cut')).toThrow(
      /Nothing would be left/,
    );
  });

  it('survives saving: the cut model reopens closed', async () => {
    const root = scene(mesh(new THREE.SphereGeometry(1, 32, 16)));
    cutCommand(root, new THREE.Plane(new THREE.Vector3(1, 0, 0), -0.3), 'Cut').command.apply();
    const reopened = await parseGlb(await exportGlb(root));
    expect(watertight(reopened.root)).toBe(true);
    expect(volume(reopened.root)).toBeCloseTo(volume(root), 6);
  });
});

describe('arranging the model', () => {
  it('places the model on the ground and centres it, as one undo step each', () => {
    const a = mesh(new THREE.BoxGeometry(1, 1, 1));
    a.position.set(3, 2, -1);
    const b = mesh(new THREE.BoxGeometry(1, 1, 1));
    b.position.set(5, 4, -1);
    const root = scene(a, b);
    const history = new EditHistory();
    history.execute(placeOnGroundCommand(root));
    expect(measure(root).min.y).toBeCloseTo(0, 9);
    expect(a.position.x).toBe(3);
    expect(() => placeOnGroundCommand(root)).toThrow(EditError);
    history.execute(centreOnOriginCommand(root));
    const centre = measure(root).getCenter(new THREE.Vector3());
    expect(centre.x).toBeCloseTo(0, 9);
    expect(centre.z).toBeCloseTo(0, 9);
    expect(measure(root).min.y).toBeCloseTo(0, 9);
    history.undo();
    history.undo();
    expect(a.position.toArray()).toEqual([3, 2, -1]);
  });

  it('mirrors a part in place and the whole model, and the saved file keeps it', async () => {
    const wedge = mesh(new THREE.ConeGeometry(0.5, 1, 3), 'Wedge');
    wedge.position.set(2, 0, 0);
    wedge.rotation.y = 0.3;
    const root = scene(wedge);
    const before = triangles(root);
    const centreX = measure(wedge).getCenter(new THREE.Vector3()).x;
    mirrorCommand(root, wedge, 'x').apply();
    const after = triangles(root);
    after.forEach((tri, t) =>
      tri.forEach((corner, c) => {
        expect(corner.x).toBeCloseTo(2 * centreX - before[t]![c]!.x, 6);
        expect(corner.y).toBeCloseTo(before[t]![c]!.y, 6);
      }),
    );
    expect(volume(root)).toBeLessThan(0); // stored as a negative scale…
    const reopened = await parseGlb(await exportGlb(root));
    expect(measure(reopened.root).min.x).toBeCloseTo(measure(root).min.x, 6);
    mirrorCommand(root, root, 'y').apply();
    expect(measure(root).min.y).toBeCloseTo(-0.5, 6);
  });

  it('deletes a part and restores it in its place', () => {
    const parts = ['A', 'B', 'C'].map((name) => mesh(new THREE.BoxGeometry(), name));
    const root = scene(...parts);
    const history = new EditHistory();
    history.execute(deleteCommand(parts[1]!));
    expect(root.children.map((c) => c.name)).toEqual(['A', 'C']);
    expect(history.changes()).toEqual(['Deleted "B"']);
    history.undo();
    expect(root.children.map((c) => c.name)).toEqual(['A', 'B', 'C']);
    expect(parts[1]!.parent).toBe(root);
    expect(() => deleteCommand(root)).toThrow(EditError);
  });

  it('moves a pivot to the centre without moving the part or its twin', () => {
    const geometry = new THREE.BoxGeometry(1, 1, 1).translate(3, 0, 0);
    const part = mesh(geometry);
    part.rotation.y = Math.PI / 2;
    const twin = mesh(geometry, 'Twin');
    const root = scene(part, twin);
    const before = triangles(root);
    const history = new EditHistory();
    history.execute(pivotToCentreCommand(part));
    triangles(root).forEach((tri, t) =>
      tri.forEach((corner, c) => expect(corner.distanceTo(before[t]![c]!)).toBeLessThan(1e-9)),
    );
    expect(part.position.x).toBeCloseTo(0, 9);
    expect(part.position.z).toBeCloseTo(-3, 9);
    expect(twin.geometry).toBe(geometry);
    history.undo();
    expect(part.geometry).toBe(geometry);
    expect(part.position.length()).toBe(0);
  });
});

describe('textures', () => {
  it('adds and replaces a base-colour texture, undoably', () => {
    const material = new THREE.MeshStandardMaterial({ name: 'Paint', color: 0xff0000 });
    const image = { width: 64, height: 32 } as unknown as ImageBitmap;
    const history = new EditHistory();
    history.execute(textureCommand(material, image, 'wood.png'));
    expect(material.map?.image).toBe(image);
    expect(material.map?.flipY).toBe(false);
    expect(material.map?.colorSpace).toBe(THREE.SRGBColorSpace);
    expect(material.color.getHex()).toBe(0xffffff); // the texture shows as it is
    const first = material.map!;
    first.wrapS = THREE.MirroredRepeatWrapping;
    const second = { width: 16, height: 16 } as unknown as ImageBitmap;
    history.execute(textureCommand(material, second, 'stone.png'));
    expect(material.map?.wrapS).toBe(THREE.MirroredRepeatWrapping);
    expect(first.image).toBe(image); // the old texture is untouched for undo
    expect(history.changes()).toEqual([
      'Added a texture to "Paint"',
      'Replaced the texture of "Paint"',
    ]);
    history.undo();
    history.undo();
    expect(material.map).toBeNull();
    expect(material.color.getHex()).toBe(0xff0000);
    const huge = { width: MAX_TEXTURE_SIZE + 1, height: 10 } as unknown as ImageBitmap;
    expect(() => textureCommand(material, huge, 'huge.png')).toThrow(/at most 8192/);
  });

  it('lists texture coordinates per material and knows when a texture can apply', () => {
    const plane = mesh(new THREE.PlaneGeometry(1, 1, 2, 2));
    const root = scene(plane);
    const material = plane.material as THREE.Material;
    expect(canTexture(root, material)).toBe(true);
    const { uv, total } = uvTriangles(root, material, 5, (m) => [m.material as THREE.Material]);
    expect(total).toBe(8);
    expect(uv).toHaveLength(5 * 6);
    plane.geometry.deleteAttribute('uv');
    expect(canTexture(root, material)).toBe(false);
  });
});
