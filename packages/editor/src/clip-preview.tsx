'use client';

import { measure } from '@ai3d/viewer';
import { useThree } from '@react-three/fiber';
import { useEffect, useMemo } from 'react';
import * as THREE from 'three';

/**
 * Inside the canvas: hides everything on the removed side (renderer clipping,
 * nothing in the model changes) and shows the plane.
 */
export function ClipPreview({
  plane,
  root,
  revision,
}: {
  plane: THREE.Plane;
  root: THREE.Object3D;
  /** Bump after edits so the plane is re-sized. */
  revision: number;
}) {
  const get = useThree((state) => state.get);
  useEffect(() => {
    const { gl, invalidate } = get();
    gl.clippingPlanes = [plane];
    invalidate();
    return () => {
      gl.clippingPlanes = [];
      invalidate();
    };
  }, [get, plane]);
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `revision` signals in-place edits
  const box = useMemo(() => measure(root), [root, revision]);
  if (box.isEmpty()) return null;
  const size = box.getSize(new THREE.Vector3()).length() * 1.15;
  // Slightly on the kept side, so the clipping does not hide the plane itself.
  const position = plane
    .projectPoint(box.getCenter(new THREE.Vector3()), new THREE.Vector3())
    .addScaledVector(plane.normal, size * 1e-4);
  const quaternion = new THREE.Quaternion().setFromUnitVectors(
    new THREE.Vector3(0, 0, 1),
    plane.normal,
  );
  return (
    <mesh position={position} quaternion={quaternion} renderOrder={10}>
      <planeGeometry args={[size, size]} />
      <meshBasicMaterial
        color="#0a84ff"
        transparent
        opacity={0.14}
        side={THREE.DoubleSide}
        depthWrite={false}
      />
    </mesh>
  );
}
