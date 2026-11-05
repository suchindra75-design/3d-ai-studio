import * as THREE from 'three';

import { CAMERA_PRESETS, frameBox, gridCellSize } from './camera';
import { applyDisplayMode, sourceMaterials, withSourceMaterials } from './display';
import {
  PRECISE_BOUNDS_LIMIT,
  buildHierarchy,
  formatLength,
  formatUnits,
  measure,
  sceneStats,
} from './inspect';

function sampleScene() {
  const texture = new THREE.Texture();
  const painted = new THREE.MeshStandardMaterial({ name: 'Glaze', map: texture });
  const plain = new THREE.MeshStandardMaterial({ name: 'Clay' });
  const body = new THREE.Mesh(new THREE.BoxGeometry(1, 2, 1), painted); // 24 vertices, 12 triangles
  body.name = 'Body';
  const handle = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.5, 0.2), painted);
  handle.name = 'Handle';
  handle.position.set(0.7, 0, 0);
  const lid = new THREE.Mesh(new THREE.BoxGeometry(1, 0.1, 1), plain);
  lid.position.set(0, 1.05, 0);
  const group = new THREE.Group();
  group.name = 'Cup';
  group.add(body, handle);
  const root = new THREE.Group();
  root.name = 'Model';
  root.add(group, lid);
  return { root, body, handle, lid, painted, plain };
}

describe('sceneStats', () => {
  it('counts meshes, vertices, triangles and unique materials and textures', () => {
    const { root } = sampleScene();
    expect(sceneStats(root)).toEqual({
      meshes: 3,
      vertices: 72,
      triangles: 36,
      materials: 2,
      textures: 1,
    });
  });

  it('counts non-indexed geometry and instances', () => {
    const geometry = new THREE.BoxGeometry().toNonIndexed(); // 36 vertices, 12 triangles
    const instanced = new THREE.InstancedMesh(geometry, new THREE.MeshBasicMaterial(), 5);
    expect(sceneStats(instanced)).toMatchObject({ meshes: 1, vertices: 180, triangles: 60 });
  });
});

describe('buildHierarchy', () => {
  it('mirrors the scene graph with subtree triangle counts and fallback names', () => {
    const { root, lid } = sampleScene();
    const tree = buildHierarchy(root);
    expect(tree.name).toBe('Model');
    expect(tree.triangles).toBe(36);
    expect(tree.children.map((c) => [c.name, c.kind, c.triangles])).toEqual([
      ['Cup', 'group', 24],
      ['Mesh', 'mesh', 12], // unnamed lid
    ]);
    expect(tree.children[1]!.id).toBe(lid.uuid);
    expect(tree.children[0]!.children.map((c) => c.name)).toEqual(['Body', 'Handle']);
  });
});

describe('measure', () => {
  it('uses exact vertex bounds in world space', () => {
    const { root } = sampleScene();
    const size = measure(root).getSize(new THREE.Vector3());
    expect(size.x).toBeCloseTo(1.3); // body -0.5 … handle 0.8
    expect(size.y).toBeCloseTo(2.1); // body -1 … lid 1.1
    expect(size.z).toBeCloseTo(1);
  });

  it('follows edits to transforms', () => {
    const { root } = sampleScene();
    root.scale.setScalar(2);
    expect(measure(root).getSize(new THREE.Vector3()).y).toBeCloseTo(4.2);
  });

  it('is not inflated by rotation', () => {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1));
    mesh.rotation.y = Math.PI / 4;
    // A loose (bounding-box-of-bounding-box) estimate would give 1.414 on both axes.
    expect(measure(mesh).getSize(new THREE.Vector3()).x).toBeCloseTo(Math.SQRT2);
    expect(measure(mesh).getSize(new THREE.Vector3()).y).toBeCloseTo(1);
  });
});

it('formats unscaled lengths without pretending they are metres', () => {
  expect(formatUnits(6.3412)).toBe('6.34 units');
  expect(formatUnits(0.3)).toBe('0.3 units');
  expect(formatUnits(0)).toBe('0 units');
});

describe('formatLength', () => {
  it.each([
    [0, '0 m'],
    [0.0042, '4.20 mm'],
    [0.123, '12.3 cm'],
    [1.5, '1.50 m'],
    [2500, '2.50 km'],
  ])('%s m → %s', (metres, text) => {
    expect(formatLength(metres)).toBe(text);
  });
});

describe('display modes', () => {
  it('swaps materials for viewing and always reports the real ones', async () => {
    const { root, body, painted } = sampleScene();
    applyDisplayMode(root, 'solid');
    expect(body.material).not.toBe(painted);
    expect(sourceMaterials(body)).toEqual([painted]);
    // Stats ignore the view material.
    expect(sceneStats(root, sourceMaterials).materials).toBe(2);

    const seen = await withSourceMaterials(root, async () => body.material);
    expect(seen).toBe(painted);
    expect(body.material).not.toBe(painted); // view restored afterwards

    applyDisplayMode(root, 'wireframe');
    expect((body.material as THREE.Material as THREE.MeshBasicMaterial).wireframe).toBe(true);
    applyDisplayMode(root, 'material');
    expect(body.material).toBe(painted);
  });
});

describe('camera framing', () => {
  it('places the camera so the bounding sphere fits the view', () => {
    const box = new THREE.Box3(new THREE.Vector3(-1, 0, -1), new THREE.Vector3(1, 2, 1));
    const framing = frameBox(box, 'front', 40, 1.5);
    expect(framing.target.toArray()).toEqual([0, 1, 0]);
    expect(framing.position.x).toBeCloseTo(0);
    expect(framing.position.y).toBeCloseTo(1);
    const radius = Math.sqrt(3);
    const distance = framing.position.distanceTo(framing.target);
    expect(distance * Math.sin(THREE.MathUtils.degToRad(20))).toBeGreaterThanOrEqual(radius);
    expect(framing.near).toBeLessThan(distance - radius);
    expect(framing.far).toBeGreaterThan(distance + radius);
  });

  it('accounts for a narrow (portrait) viewport', () => {
    const box = new THREE.Box3(new THREE.Vector3(-1, -1, -1), new THREE.Vector3(1, 1, 1));
    const wide = frameBox(box, 'iso', 40, 2).position.length();
    const tall = frameBox(box, 'iso', 40, 0.5).position.length();
    expect(tall).toBeGreaterThan(wide);
  });

  it('has a preset for every direction', () => {
    const box = new THREE.Box3(new THREE.Vector3(-1, -1, -1), new THREE.Vector3(1, 1, 1));
    for (const { value } of CAMERA_PRESETS) {
      const { position } = frameBox(box, value, 40, 1);
      expect(Number.isFinite(position.length())).toBe(true);
    }
    expect(frameBox(box, 'top', 40, 1).position.y).toBeGreaterThan(3);
  });

  it('picks grid cells from the model size', () => {
    const box = (extent: number) =>
      new THREE.Box3(new THREE.Vector3(0, 0, 0), new THREE.Vector3(extent, 1, extent));
    expect(gridCellSize(box(1))).toBeCloseTo(0.1);
    expect(gridCellSize(box(0.25))).toBeCloseTo(0.01);
    expect(gridCellSize(box(30))).toBeCloseTo(1);
    expect(gridCellSize(new THREE.Box3())).toBe(0.1);
  });
});

describe('view-only display modes and large-model bounds', () => {
  it('shows normals and a UV checker without touching the model’s own materials', () => {
    const own = new THREE.MeshStandardMaterial({ name: 'Paint' });
    const mapped = new THREE.Mesh(new THREE.BoxGeometry(), own);
    const unmapped = new THREE.Mesh(new THREE.BoxGeometry(), own);
    unmapped.geometry.deleteAttribute('uv');
    const root = new THREE.Group().add(mapped, unmapped);
    applyDisplayMode(root, 'normals');
    expect((mapped.material as THREE.Material).type).toBe('MeshNormalMaterial');
    applyDisplayMode(root, 'uv');
    expect((mapped.material as unknown as THREE.MeshBasicMaterial).map).not.toBeNull();
    expect((unmapped.material as THREE.Material).name).toBe('Solid view'); // no UVs: solid
    expect(sourceMaterials(mapped)).toEqual([own]);
    applyDisplayMode(root, 'material');
    expect(mapped.material).toBe(own);
  });

  it('shades parts without normals flat in the Solid and Normals views', () => {
    const geometry = new THREE.BoxGeometry();
    geometry.deleteAttribute('normal');
    const bare = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial());
    const normal = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshStandardMaterial());
    const root = new THREE.Group().add(bare, normal);
    for (const mode of ['solid', 'normals'] as const) {
      applyDisplayMode(root, mode);
      expect((bare.material as THREE.MeshStandardMaterial).flatShading).toBe(true);
      expect((normal.material as THREE.MeshStandardMaterial).flatShading).toBe(false);
    }
    bare.geometry.deleteAttribute('uv');
    applyDisplayMode(root, 'uv');
    expect((bare.material as THREE.MeshStandardMaterial).flatShading).toBe(true); // solid fallback
  });

  it('measures exactly below the vertex limit and from part boxes above it', () => {
    const small = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1));
    small.rotation.y = Math.PI / 4;
    const exact = measure(new THREE.Group().add(small));
    expect(exact.max.x).toBeCloseTo(Math.SQRT1_2, 6); // corners, not the rotated box
    // A circle of more vertices than the limit, turned 45° about Z: exact bounds would
    // still reach x = 1, the turned bounding square reaches x = √2.
    const count = PRECISE_BOUNDS_LIMIT + 1;
    const ring = new Float32Array(count * 3);
    for (let i = 0; i < count; i += 1) {
      const angle = (i / count) * Math.PI * 2;
      ring[i * 3] = Math.cos(angle);
      ring[i * 3 + 1] = Math.sin(angle);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(ring, 3));
    const big = new THREE.Mesh(geometry);
    big.rotation.z = Math.PI / 4;
    const approximate = measure(new THREE.Group().add(big));
    expect(approximate.max.x).toBeCloseTo(Math.SQRT2, 4);
  });
});
