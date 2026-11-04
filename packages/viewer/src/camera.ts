import * as THREE from 'three';

export type CameraPreset = 'iso' | 'front' | 'back' | 'left' | 'right' | 'top' | 'bottom';

export const CAMERA_PRESETS: { value: CameraPreset; label: string; key: string }[] = [
  { value: 'iso', label: 'Fit (3/4 view)', key: '0' },
  { value: 'front', label: 'Front', key: '1' },
  { value: 'back', label: 'Back', key: '2' },
  { value: 'left', label: 'Left', key: '3' },
  { value: 'right', label: 'Right', key: '4' },
  { value: 'top', label: 'Top', key: '5' },
  { value: 'bottom', label: 'Bottom', key: '6' },
];

// glTF is Y-up and faces +Z. A tiny offset keeps top/bottom views off the
// orbit controls' pole, where the up vector is undefined.
const DIRECTIONS: Record<CameraPreset, [number, number, number]> = {
  iso: [1, 0.7, 1.2],
  front: [0, 0, 1],
  back: [0, 0, -1],
  left: [-1, 0, 0],
  right: [1, 0, 0],
  top: [0, 1, 1e-4],
  bottom: [0, -1, 1e-4],
};

export interface Framing {
  position: THREE.Vector3;
  target: THREE.Vector3;
  near: number;
  far: number;
}

/** Camera placement that fits ``box`` in view from a preset direction. */
export function frameBox(
  box: THREE.Box3,
  preset: CameraPreset,
  fovDegrees: number,
  aspect: number,
): Framing {
  const target = box.isEmpty() ? new THREE.Vector3() : box.getCenter(new THREE.Vector3());
  const radius = box.isEmpty()
    ? 1
    : Math.max(box.getBoundingSphere(new THREE.Sphere()).radius, 1e-6);
  const vertical = THREE.MathUtils.degToRad(fovDegrees) / 2;
  const horizontal = Math.atan(Math.tan(vertical) * aspect);
  const distance = (radius / Math.sin(Math.min(vertical, horizontal))) * 1.1;
  const direction = new THREE.Vector3(...DIRECTIONS[preset]).normalize();
  return {
    position: target.clone().addScaledVector(direction, distance),
    target,
    near: Math.max(distance / 1000, radius / 1000),
    far: distance * 100,
  };
}

/** Grid spacing: the power of ten that gives roughly 10–100 cells across the model. */
export function gridCellSize(box: THREE.Box3): number {
  if (box.isEmpty()) return 0.1;
  const size = box.getSize(new THREE.Vector3());
  const extent = Math.max(size.x, size.z, 1e-6);
  return 10 ** Math.floor(Math.log10(extent / 10));
}
