/**
 * Display modes swap each mesh's material for viewing only. The model's own
 * materials are remembered per mesh, so edits and exports always work on the
 * real materials, whatever the viewer shows.
 */
import * as THREE from 'three';

import { materialsOf } from './inspect';

export type DisplayMode = 'material' | 'solid' | 'wireframe';

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
    mesh.material = mode === 'material' ? material : mode === 'solid' ? SOLID : WIREFRAME;
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
