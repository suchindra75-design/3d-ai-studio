'use client';

import { useEffect, useMemo } from 'react';
import * as THREE from 'three';

import type { ProblemEdges } from './problems';

/**
 * Open edges in red and non-manifold edges in orange, drawn through the
 * surface so holes on the far side are visible too. Render inside the canvas.
 */
export function ProblemOverlay({ edges }: { edges: ProblemEdges }) {
  const [open, crowded] = useMemo(
    () =>
      [edges.boundary, edges.nonManifold].map((points) => {
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute('position', new THREE.Float32BufferAttribute(points, 3));
        return geometry;
      }),
    [edges],
  );
  useEffect(
    () => () => {
      open?.dispose();
      crowded?.dispose();
    },
    [open, crowded],
  );
  return (
    <>
      <lineSegments geometry={open} renderOrder={30}>
        <lineBasicMaterial color="#ff453a" depthTest={false} transparent opacity={0.95} />
      </lineSegments>
      <lineSegments geometry={crowded} renderOrder={31}>
        <lineBasicMaterial color="#ff9f0a" depthTest={false} transparent opacity={0.95} />
      </lineSegments>
    </>
  );
}
