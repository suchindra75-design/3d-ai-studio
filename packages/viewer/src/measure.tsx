'use client';

import { Html, Line } from '@react-three/drei';
import type * as THREE from 'three';

/**
 * Measurement points on the model's surface and the line between them, drawn
 * on top of the model. ``size`` is the marker radius in scene units.
 */
export function MeasureMarkers({
  points,
  label,
  size,
}: {
  points: THREE.Vector3[];
  label: string | null;
  size: number;
}) {
  const [a, b] = points;
  return (
    <group>
      {points.map((point, i) => (
        <mesh key={i} position={point} renderOrder={20}>
          <sphereGeometry args={[size, 16, 12]} />
          <meshBasicMaterial color="#ffd60a" depthTest={false} transparent />
        </mesh>
      ))}
      {a && b ? (
        <>
          <Line points={[a, b]} color="#ffd60a" lineWidth={2} depthTest={false} renderOrder={19} />
          {label ? (
            <Html position={a.clone().lerp(b, 0.5)} center zIndexRange={[20, 0]}>
              <div
                style={{
                  background: 'rgba(14,14,16,0.85)',
                  color: '#ffd60a',
                  font: '600 11px/1.4 system-ui, sans-serif',
                  padding: '2px 6px',
                  borderRadius: 4,
                  whiteSpace: 'nowrap',
                  pointerEvents: 'none',
                }}
              >
                {label}
              </div>
            </Html>
          ) : null}
        </>
      ) : null}
    </group>
  );
}
