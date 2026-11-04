/** Replacing a material's base-colour texture, and reading texture coordinates for the UV view. */
import * as THREE from 'three';

import { EditError } from './arrange';
import { materialName, type PbrMaterial } from './commands';
import type { Command } from './history';

/** Larger images are refused: most GPUs and glTF viewers stop at 8192 px. */
export const MAX_TEXTURE_SIZE = 8192;

type Image = { width: number; height: number };

/**
 * Sets ``image`` as the material's base-colour texture. An existing texture
 * keeps its settings (wrapping, filtering, UV channel); a new one gets glTF's
 * defaults. Only meaningful when the parts using it have texture coordinates.
 */
export function textureCommand(
  material: PbrMaterial,
  image: TexImageSource & Image,
  fileName: string,
  /** Written into the saved file as this type (PNG or JPEG). */
  mimeType = 'image/png',
): Command {
  if (image.width > MAX_TEXTURE_SIZE || image.height > MAX_TEXTURE_SIZE) {
    throw new EditError(
      `The image is ${image.width} × ${image.height} px; textures can be at most ${MAX_TEXTURE_SIZE} px on each side.`,
    );
  }
  const before = material.map;
  const after = before ? before.clone() : new THREE.Texture();
  after.source = new THREE.Source(image);
  after.name = fileName;
  after.userData = { ...after.userData, mimeType };
  after.flipY = false; // glTF texture coordinates start at the top of the image
  after.colorSpace = THREE.SRGBColorSpace;
  after.needsUpdate = true;
  // Colour multiplies the texture; keep it unless there was no texture before.
  const colorBefore = material.color.clone();
  const set = (map: THREE.Texture | null, color: THREE.Color) => {
    const rebuild = (material.map === null) !== (map === null);
    material.map = map;
    material.color.copy(color);
    if (rebuild) material.needsUpdate = true;
  };
  const colorAfter = before ? colorBefore : new THREE.Color(1, 1, 1);
  return {
    label: `${before ? 'Replaced the texture of' : 'Added a texture to'} "${materialName(material)}"`,
    apply: () => set(after, colorAfter),
    revert: () => set(before, colorBefore),
  };
}

/** True when every part using ``material`` has texture coordinates for it. */
export function canTexture(root: THREE.Object3D, material: THREE.Material): boolean {
  let users = 0;
  let mapped = 0;
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (!mesh.isMesh) return;
    const materials = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
    if (!materials.includes(material)) return;
    users += 1;
    if (mesh.geometry.getAttribute('uv')) mapped += 1;
  });
  return users > 0 && users === mapped;
}

/**
 * The texture-coordinate triangles of the meshes under ``root`` that use
 * ``material``: flat [u0, v0, u1, v1, u2, v2, …], at most ``limit`` triangles.
 */
export function uvTriangles(
  root: THREE.Object3D,
  material: THREE.Material,
  limit: number,
  own: (mesh: THREE.Mesh) => THREE.Material[],
): { uv: Float32Array; total: number } {
  const chunks: number[] = [];
  let total = 0;
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (!mesh.isMesh) return;
    const uv = mesh.geometry.getAttribute('uv');
    if (!uv) return;
    const materials = own(mesh);
    const index = mesh.geometry.index;
    const count = index ? index.count : uv.count;
    const groups =
      mesh.geometry.groups.length > 0
        ? mesh.geometry.groups
        : [{ start: 0, count, materialIndex: 0 }];
    for (const group of groups) {
      if (materials[group.materialIndex ?? 0] !== material) continue;
      const end = Math.min(group.start + group.count, count);
      for (let k = group.start; k + 2 < end; k += 3) {
        total += 1;
        if (total > limit) continue;
        for (let c = 0; c < 3; c += 1) {
          const i = index ? index.getX(k + c) : k + c;
          chunks.push(uv.getX(i), uv.getY(i));
        }
      }
    }
  });
  return { uv: new Float32Array(chunks), total };
}
