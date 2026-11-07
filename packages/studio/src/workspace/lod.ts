import type { ApiClient } from '@ai3d/api-client';
import type { LodRead } from '@ai3d/shared-types';
import { disposeObject, parseGlb, type LoadedModel, type LodSource } from '@ai3d/viewer';
import { useEffect, useRef, useState } from 'react';
import * as THREE from 'three';

const POLL_MS = 2000;

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
 *
 * The coarsest level is loaded first (a preview while the full file downloads; it
 * is small). The finer levels wait until ``fullReady``, so on a slow connection they
 * do not take bandwidth from the full model; each is added as it arrives.
 */
export function useLevelsOfDetail(
  api: ApiClient,
  versionId: string,
  enabled: boolean,
  fullReady: boolean,
): LodState {
  const [result, setResult] = useState<{ key: string; state: LodState } | null>(null);
  const key = `${versionId}:${enabled}`;
  // The effect below runs once per version; it waits on this for the finer levels.
  const fullReadyRef = useRef(fullReady);
  const waiters = useRef<(() => void)[]>([]);
  useEffect(() => {
    fullReadyRef.current = fullReady;
    if (fullReady) for (const wake of waiters.current.splice(0)) wake();
  }, [fullReady]);

  useEffect(() => {
    if (!enabled) return undefined;
    const controller = new AbortController();
    const loaded: LoadedModel[] = [];
    const pending = waiters.current;
    const report = (state: LodState) => {
      if (!controller.signal.aborted) setResult({ key, state });
    };
    const fetchLevel = async (url: string): Promise<LoadedModel> => {
      const response = await fetch(api.url(url), { signal: controller.signal });
      if (!response.ok) throw new Error(`The backend returned ${response.status}.`);
      const model = await parseGlb(await response.arrayBuffer());
      if (controller.signal.aborted) {
        disposeObject(model.root);
        throw new DOMException('Aborted', 'AbortError');
      }
      freeTextures(model.root);
      loaded.push(model);
      return model;
    };
    void (async () => {
      let info = await api.lod.get(versionId);
      if (info.status === 'missing') {
        report({ status: 'preparing' });
        info = await api.lod.build(versionId);
      }
      // Built in the background on the backend (minutes for the largest models).
      while (info.status === 'building') {
        report({ status: 'preparing' });
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
        if (controller.signal.aborted) return;
        info = await api.lod.get(versionId);
      }
      if (info.status === 'unavailable' || info.status === 'failed') {
        report({ status: 'unavailable', message: info.message ?? 'Not available.' });
        return;
      }
      if (info.status !== 'ready' || info.levels.length === 0) {
        report({ status: 'off' });
        return;
      }
      report({ status: 'preparing' });
      // Coarsest first; finer levels after the full model, finest last.
      const levels = [...info.levels].sort((a, b) => a.triangles - b.triangles);
      const sources: LodSource[] = [];
      const publish = () =>
        report({
          status: 'ready',
          fullTriangles: info.triangles,
          // Finest first, as the viewer expects.
          sources: [...sources].sort((a, b) => b.triangles - a.triangles),
          info,
        });
      for (const [index, level] of levels.entries()) {
        if (index === 1 && !fullReadyRef.current) {
          await new Promise<void>((resolve) => pending.push(resolve));
        }
        if (controller.signal.aborted) return;
        const model = await fetchLevel(level.file_url);
        sources.push({ triangles: level.triangles, root: model.root });
        publish();
      }
    })().catch((cause: unknown) => {
      if (
        controller.signal.aborted ||
        (cause instanceof DOMException && cause.name === 'AbortError')
      ) {
        return;
      }
      // Viewing still works in full detail; say why the faster views are missing.
      report({
        status: 'unavailable',
        message: cause instanceof Error ? cause.message : 'The simplified views could not load.',
      });
    });
    return () => {
      controller.abort();
      for (const wake of pending.splice(0)) wake();
      for (const model of loaded) disposeObject(model.root);
    };
  }, [api, versionId, enabled, key]);

  if (!enabled || result?.key !== key) return { status: 'off' };
  return result.state;
}
