import type { ApiClient } from '@ai3d/api-client';
import type { LodRead } from '@ai3d/shared-types';
import { disposeObject, parseGlb, type LoadedModel, type LodSource } from '@ai3d/viewer';
import { useEffect, useState } from 'react';
import * as THREE from 'three';

export type LodState =
  | { status: 'off' }
  | { status: 'preparing' }
  | { status: 'ready'; fullTriangles: number; sources: LodSource[]; info: LodRead }
  | { status: 'unavailable'; message: string };

/** The levels' own materials are never drawn (copies use the full model's), so their
 * textures are freed as soon as they are loaded. */
function freeTextures(root: THREE.Object3D): void {
  root.traverse((object) => {
    const mesh = object as THREE.Mesh;
    if (!mesh.isMesh) return;
    for (const material of Array.isArray(mesh.material) ? mesh.material : [mesh.material]) {
      for (const value of Object.values(material)) {
        if (value instanceof THREE.Texture) value.dispose();
      }
    }
  });
}

/**
 * Levels of detail for a version (D58): built on the backend the first time a large
 * version is opened, then loaded here. Freed when the version changes.
 */
export function useLevelsOfDetail(api: ApiClient, versionId: string, enabled: boolean): LodState {
  const [result, setResult] = useState<{ key: string; state: LodState } | null>(null);
  const key = `${versionId}:${enabled}`;

  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    let loaded: LoadedModel[] = [];
    const report = (state: LodState) => {
      if (!controller.signal.aborted) setResult({ key, state });
    };
    void (async () => {
      let info = await api.lod.get(versionId);
      if (info.status === 'missing') {
        report({ status: 'preparing' });
        info = await api.lod.build(versionId);
      }
      if (info.status === 'unavailable') {
        report({ status: 'unavailable', message: info.message ?? 'Not available.' });
        return;
      }
      if (info.status !== 'ready' || info.levels.length === 0) {
        report({ status: 'off' });
        return;
      }
      report({ status: 'preparing' });
      const models = await Promise.all(
        info.levels.map(async (level) => {
          const response = await fetch(api.url(level.file_url), { signal: controller.signal });
          if (!response.ok) throw new Error(`The backend returned ${response.status}.`);
          return parseGlb(await response.arrayBuffer());
        }),
      );
      if (controller.signal.aborted) {
        for (const model of models) disposeObject(model.root);
        return;
      }
      for (const model of models) freeTextures(model.root);
      loaded = models;
      report({
        status: 'ready',
        fullTriangles: info.triangles,
        sources: models.map((model, index) => ({
          triangles: info.levels[index]!.triangles,
          root: model.root,
        })),
        info,
      });
    })().catch((cause: unknown) => {
      if (controller.signal.aborted) return;
      // Viewing still works in full detail; say why the faster views are missing.
      report({
        status: 'unavailable',
        message: cause instanceof Error ? cause.message : 'The simplified views could not load.',
      });
    });
    return () => {
      controller.abort();
      for (const model of loaded) disposeObject(model.root);
    };
  }, [api, versionId, enabled, key]);

  if (!enabled || result?.key !== key) return { status: 'off' };
  return result.state;
}
