'use client';

import { displayName } from '@ai3d/viewer';
import { TransformControls } from '@react-three/drei';
import { useRef } from 'react';
import type * as THREE from 'three';

import {
  captureTransform,
  describeTransform,
  sameTransform,
  transformCommand,
  type TransformState,
} from './commands';
import type { EditHistory } from './history';

export type GizmoMode = 'translate' | 'rotate' | 'scale';

/**
 * On-canvas handles for the selected object. A drag becomes one undoable
 * command when the pointer is released. Render inside the viewer's canvas.
 */
export function TransformGizmo({
  object,
  mode,
  history,
}: {
  object: THREE.Object3D;
  mode: GizmoMode;
  history: EditHistory;
}) {
  const before = useRef<TransformState | null>(null);
  return (
    <TransformControls
      object={object}
      mode={mode}
      size={0.85}
      onMouseDown={() => {
        before.current = captureTransform(object);
      }}
      onMouseUp={() => {
        const start = before.current;
        before.current = null;
        if (!start) return;
        const end = captureTransform(object);
        if (sameTransform(start, end)) return;
        history.record(
          transformCommand(object, start, end, describeTransform(displayName(object), start, end)),
        );
      }}
    />
  );
}
