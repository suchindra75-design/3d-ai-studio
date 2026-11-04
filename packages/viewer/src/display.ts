/**
 * Display modes swap each mesh's material for viewing only. The model's own
 * materials are remembered per mesh, so edits and exports always work on the
 * real materials, whatever the viewer shows.
 */
import * as THREE from 'three';

import { materialsOf } from './inspect';

export type DisplayMode = 'material' | 'solid' | 'wireframe' | 'normals' | 'uv';

const SOLID = new THREE.MeshStandardMaterial({
  name: 'Solid view',
  color: 0xb4b4b8,
  roughness: 0.65,
  metalness: 0,
});
const WIREFRAME = new THREE.MeshBasicMaterial({
  name: 'Wireframe view',
  color: 0x8fb8ff,
  wireframe: true,
});

// Surface direction as colour: shows flipped or rough patches at a glance.
const NORMALS = new THREE.MeshNormalMaterial({ name: 'Normals view' });

/** An 8 × 8 checker with numbered-looking corners, to judge texture stretching and seams. */
function checkerTexture(): THREE.Texture {
  const size = 256;
  const cells = 8;
  const data = new Uint8Array(size * size * 4);
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const cx = Math.floor((x * cells) / size);
      const cy = Math.floor((y * cells) / size);
      const dark = (cx + cy) % 2 === 0;
      // A colour ramp across U (red) and V (green) shows orientation and mirroring.
      const i = (y * size + x) * 4;
      data[i] = dark ? 40 + cx * 20 : 200 + cx * 6;
      data[i + 1] = dark ? 40 + cy * 20 : 200 + cy * 6;
      data[i + 2] = dark ? 60 : 230;
      data[i + 3] = 255;
    }
  }
  const texture = new THREE.DataTexture(data, size, size);
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.RepeatWrapping;
  texture.magFilter = THREE.NearestFilter;
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.flipY = false; // like glTF textures: v = 0 is the top row
  texture.needsUpdate = true;
  return texture;
}

let uvChecker: THREE.MeshBasicMaterial | null = null;
function uvMaterial(): THREE.MeshBasicMaterial {
  uvChecker ??= new THREE.MeshBasicMaterial({ name: 'UV checker view', map: checkerTexture() });
  return uvChecker;
}

function viewMaterial(mesh: THREE.Mesh, mode: Exclude<DisplayMode, 'material'>): THREE.Material {
  if (mode === 'solid') return SOLID;
  if (mode === 'wireframe') return WIREFRAME;
  if (mode === 'normals') return NORMALS;
  // Parts without texture coordinates cannot show a checker: they show solid.
  return mesh.geometry.getAttribute('uv') ? uvMaterial() : SOLID;
}

const own = new WeakMap<THREE.Mesh, THREE.Material | THREE.Material[]>();

function remember(mesh: THREE.Mesh): THREE.Material | THREE.Material[] {
  let material = own.get(mesh);
  if (material === undefined) {
    material = mesh.material;
    own.set(mesh, material);
  }
  return material;
}

export function applyDisplayMode(root: THREE.Object3D, mode: DisplayMode): void {
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (!mesh.isMesh) return;
    const material = remember(mesh);
    mesh.material = mode === 'material' ? material : viewMaterial(mesh, mode);
  });
}

/** The mesh's own materials, regardless of the current display mode. */
export function sourceMaterials(mesh: THREE.Mesh): THREE.Material[] {
  const material = own.get(mesh) ?? mesh.material;
  return Array.isArray(material) ? material : [material];
}

/** Runs ``fn`` with every mesh showing its own materials (for exporting), then restores the view. */
export async function withSourceMaterials<T>(
  root: THREE.Object3D,
  fn: () => Promise<T>,
): Promise<T> {
  const shown = new Map<THREE.Mesh, THREE.Material | THREE.Material[]>();
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (!mesh.isMesh) return;
    shown.set(mesh, mesh.material);
    mesh.material = own.get(mesh) ?? mesh.material;
  });
  try {
    return await fn();
  } finally {
    for (const [mesh, material] of shown) mesh.material = material;
  }
}

export { materialsOf };
