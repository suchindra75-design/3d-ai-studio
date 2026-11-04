import * as THREE from 'three';
import { DRACOLoader } from 'three/examples/jsm/loaders/DRACOLoader.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';

export interface LoadedModel {
  root: THREE.Group;
  /** glTF extensions the file declares (e.g. KHR_materials_*). */
  extensions: string[];
  generator: string | null;
  /** Kept so that saving an edited version does not drop them. */
  animations: THREE.AnimationClip[];
}

/** Extensions three.js cannot decode without extra transcoders we do not ship yet. */
const UNSUPPORTED = ['KHR_texture_basisu'];

export class ModelLoadError extends Error {
  override name = 'ModelLoadError';
}

/** Parses a GLB (binary glTF) file that is already in memory. */
export async function parseGlb(data: ArrayBuffer): Promise<LoadedModel> {
  const loader = new GLTFLoader();
  // three ships the Draco decoder as bundler assets (new URL(…, import.meta.url)),
  // so Vite and Next serve it from the app itself; nothing comes from a CDN.
  const draco = new DRACOLoader();
  loader.setDRACOLoader(draco);
  loader.setMeshoptDecoder(MeshoptDecoder);
  try {
    const gltf = await loader.parseAsync(data, '');
    const json = gltf.parser.json as {
      extensionsUsed?: string[];
      extensionsRequired?: string[];
      asset?: { generator?: string };
    };
    const extensions = json.extensionsUsed ?? [];
    const root = gltf.scene;
    // The scene's own name in the file carries no meaning (exporters write "AuxScene").
    root.name = 'Model';
    root.traverse((object) => {
      if ((object as THREE.Mesh).isMesh) {
        object.castShadow = true;
        object.receiveShadow = true;
      }
    });
    return {
      root,
      extensions,
      generator: json.asset?.generator ?? null,
      animations: gltf.animations,
    };
  } catch (cause) {
    const message = cause instanceof Error ? cause.message : String(cause);
    const missing = UNSUPPORTED.find((ext) => message.includes(ext) || message.includes('KTX2'));
    throw new ModelLoadError(
      missing
        ? `This file uses ${missing} (compressed textures), which the viewer cannot decode yet.`
        : `The file could not be opened as a GLB model: ${message}`,
      { cause },
    );
  } finally {
    draco.dispose();
  }
}

/** Frees GPU resources held by a model that is no longer shown. */
export function disposeObject(root: THREE.Object3D): void {
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (!mesh.isMesh) return;
    mesh.geometry.dispose();
    for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
      for (const value of Object.values(material)) {
        if (value instanceof THREE.Texture) value.dispose();
      }
      material.dispose();
    }
  });
}
