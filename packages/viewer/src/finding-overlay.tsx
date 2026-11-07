'use client';

import { useEffect, useMemo } from 'react';
import * as THREE from 'three';

/** Where an inspection finding is (the report's world-space locations). */
export interface FindingLocations {
  points: number[][];
  segments: number[][];
  triangles: number[][];
}

/** The box around a finding's locations (null when it has none). */
export function locationsBox(locations: FindingLocations): THREE.Box3 | null {
  const box = new THREE.Box3();
  const add = (values: number[]) => {
    for (let i = 0; i + 2 < values.length; i += 3) {
      box.expandByPoint(new THREE.Vector3(values[i], values[i + 1], values[i + 2]));
    }
  };
  for (const list of [locations.points, locations.segments, locations.triangles]) {
    for (const values of list) add(values);
  }
  return box.isEmpty() ? null : box;
}

function geometryOf(lists: number[][]): THREE.BufferGeometry | null {
  if (!lists.length) return null;
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(lists.flat(), 3));
  return geometry;
}

/**
 * A finding drawn on the model (D69): points, edges and triangles in one colour,
 * through the surface so hidden ones show too. Render inside the canvas; it adds no
 * WebGL context and frees its buffers when the finding changes.
 */
export function FindingOverlay({
  locations,
  color = '#ff453a',
}: {
  locations: FindingLocations;
  color?: string;
}) {
  const [points, segments, triangles] = useMemo(
    () => [
      geometryOf(locations.points),
      geometryOf(locations.segments),
      geometryOf(locations.triangles),
    ],
    [locations],
  );
  useEffect(
    () => () => {
      points?.dispose();
      segments?.dispose();
      triangles?.dispose();
    },
    [points, segments, triangles],
  );
  return (
    <group name="Inspection finding">
      {triangles ? (
        <mesh geometry={triangles} renderOrder={32}>
          <meshBasicMaterial
            color={color}
            transparent
            opacity={0.55}
            depthTest={false}
            side={THREE.DoubleSide}
          />
        </mesh>
      ) : null}
      {segments ? (
        <lineSegments geometry={segments} renderOrder={33}>
          <lineBasicMaterial color={color} depthTest={false} transparent opacity={0.95} />
        </lineSegments>
      ) : null}
      {points ? (
        <points geometry={points} renderOrder={34}>
          <pointsMaterial
            color={color}
            size={7}
            sizeAttenuation={false}
            depthTest={false}
            transparent
          />
        </points>
      ) : null}
    </group>
  );
}
