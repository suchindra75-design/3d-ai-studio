import { withSourceMaterials } from '@ai3d/viewer';
import type * as THREE from 'three';
import { GLTFExporter } from 'three/examples/jsm/exporters/GLTFExporter.js';

/**
 * Writes the edited scene as a GLB with its own materials (never the solid or
 * wireframe view materials). Hidden parts are kept: hiding is a view aid, not
 * an edit. Compression from the source file (Draco, meshopt) is not re-applied.
 *
 * ``root`` is the loaded scene group; its children become the file's root
 * nodes, so saving and reopening never adds a wrapper level. (The root itself
 * is therefore not transformable in the workspace.)
 */
export async function exportGlb(
  root: THREE.Object3D,
  animations: THREE.AnimationClip[] = [],
): Promise<ArrayBuffer> {
  const exporter = new GLTFExporter();
  return withSourceMaterials(root, async () => {
    const result = await exporter.parseAsync(root.children, {
      binary: true,
      onlyVisible: false,
      animations,
    });
    if (!(result instanceof ArrayBuffer)) {
      throw new Error('The exporter did not produce a binary GLB.');
    }
    return result;
  });
}
