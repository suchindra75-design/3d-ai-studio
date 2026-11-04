'use client';

import { Grid, OrbitControls } from '@react-three/drei';
import { Canvas, useThree, type ThreeEvent } from '@react-three/fiber';
import { useEffect, useMemo, useRef, type ReactNode } from 'react';
import * as THREE from 'three';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';

import { frameBox, gridCellSize, type CameraPreset } from './camera';
import { applyDisplayMode, type DisplayMode } from './display';
import { measure } from './inspect';

export interface ViewRequest {
  preset: CameraPreset;
  /** Change it to re-apply the same preset. */
  nonce: number;
}

export interface ViewportOptions {
  display: DisplayMode;
  grid: boolean;
  axes: boolean;
  bounds: boolean;
  shadows: boolean;
}

export const DEFAULT_VIEWPORT_OPTIONS: ViewportOptions = {
  display: 'material',
  grid: true,
  axes: false,
  bounds: false,
  shadows: true,
};

export interface ViewportProps {
  model: THREE.Object3D;
  /** Bump after anything changes the model (edits, undo) so helpers re-measure. */
  revision: number;
  options: ViewportOptions;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  view: ViewRequest;
  /** Accessible description of what the canvas shows. */
  label: string;
  className?: string;
  /** Extra scene content, e.g. the editor's transform gizmo. */
  children?: ReactNode;
}

const SELECTION_COLOR = new THREE.Color('#0a84ff');
const BOUNDS_COLOR = new THREE.Color('#8e8e93');
// A pointer that moved more than this between down and up was orbiting, not clicking.
const CLICK_TOLERANCE_PX = 4;

/** three.js r163+ renders with WebGL 2 only. */
export function webglAvailable(): boolean {
  try {
    return document.createElement('canvas').getContext('webgl2') !== null;
  } catch {
    return false;
  }
}

/** Image-based lighting generated on the GPU from three's built-in room; nothing is downloaded. */
function RoomLighting() {
  const get = useThree((state) => state.get);
  useEffect(() => {
    const { gl, scene, invalidate } = get();
    const pmrem = new THREE.PMREMGenerator(gl);
    const room = new RoomEnvironment();
    const target = pmrem.fromScene(room, 0.04);
    scene.environment = target.texture;
    scene.environmentIntensity = 0.85;
    invalidate();
    return () => {
      scene.environment = null;
      target.dispose();
      room.dispose();
      pmrem.dispose();
    };
  }, [get]);
  return null;
}

function KeyLight({ box, shadows }: { box: THREE.Box3; shadows: boolean }) {
  const light = useRef<THREE.DirectionalLight>(null);
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(box.getBoundingSphere(new THREE.Sphere()).radius, 1e-3);
  useEffect(() => {
    const current = light.current;
    if (!current) return;
    current.target.position.copy(center);
    current.target.updateMatrixWorld();
    const camera = current.shadow.camera;
    camera.left = -radius * 1.5;
    camera.right = radius * 1.5;
    camera.top = radius * 1.5;
    camera.bottom = -radius * 1.5;
    camera.near = radius * 0.01;
    camera.far = radius * 10;
    camera.updateProjectionMatrix();
  }, [center, radius]);
  return (
    <directionalLight
      ref={light}
      castShadow={shadows}
      intensity={1.4}
      position={[center.x + radius * 2, center.y + radius * 3, center.z + radius * 1.5]}
      shadow-mapSize={[2048, 2048]}
      shadow-bias={-0.0004}
      shadow-normalBias={0.02}
    />
  );
}

function Scene({
  model,
  revision,
  options,
  selectedId,
  onSelect,
  view,
  children,
}: Omit<ViewportProps, 'label' | 'className'>) {
  const get = useThree((state) => state.get);
  const invalidate = useThree((state) => state.invalidate);
  const controls = useThree((state) => state.controls) as unknown as {
    target: THREE.Vector3;
    update: () => void;
  } | null;

  useEffect(() => {
    applyDisplayMode(model, options.display);
    invalidate();
  }, [model, options.display, invalidate]);

  // eslint-disable-next-line react-hooks/exhaustive-deps -- `revision` signals in-place edits
  const box = useMemo(() => measure(model), [model, revision]);
  const selected = useMemo(
    () => (selectedId ? (model.getObjectByProperty('uuid', selectedId) ?? null) : null),
    [model, selectedId],
  );
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `revision` signals in-place edits
  const selectedBox = useMemo(() => (selected ? measure(selected) : null), [selected, revision]);

  // Frame the model when it loads or a camera preset is requested; edits keep the camera.
  useEffect(() => {
    const { camera, size } = get();
    if (!(camera instanceof THREE.PerspectiveCamera)) return;
    const framing = frameBox(measure(model), view.preset, camera.fov, size.width / size.height);
    camera.position.copy(framing.position);
    camera.near = framing.near;
    camera.far = framing.far;
    camera.updateProjectionMatrix();
    if (controls) {
      controls.target.copy(framing.target);
      controls.update();
    } else {
      camera.lookAt(framing.target);
    }
    invalidate();
  }, [model, view.preset, view.nonce, controls, get, invalidate]);

  useEffect(() => invalidate(), [revision, options, selectedId, invalidate]);

  const radius = Math.max(box.getBoundingSphere(new THREE.Sphere()).radius, 1e-3);
  const floor = box.isEmpty() ? 0 : box.min.y;
  const center = box.getCenter(new THREE.Vector3());
  const cell = gridCellSize(box);

  function handleClick(event: ThreeEvent<MouseEvent>) {
    if (event.delta > CLICK_TOLERANCE_PX) return;
    event.stopPropagation();
    onSelect(event.object.uuid);
  }

  return (
    <>
      <RoomLighting />
      <hemisphereLight args={['#ffffff', '#3a3a40', 0.35]} />
      <KeyLight box={box} shadows={options.shadows} />
      <primitive object={model} onClick={handleClick} />
      {options.shadows ? (
        <mesh rotation-x={-Math.PI / 2} position={[center.x, floor, center.z]} receiveShadow>
          <planeGeometry args={[radius * 8, radius * 8]} />
          <shadowMaterial opacity={0.32} />
        </mesh>
      ) : null}
      {options.grid ? (
        <Grid
          position={[0, floor - radius * 1e-4, 0]}
          cellSize={cell}
          sectionSize={cell * 10}
          cellThickness={0.6}
          sectionThickness={1}
          cellColor="#2c2c31"
          sectionColor="#45454c"
          fadeDistance={radius * 25}
          fadeStrength={1.5}
          infiniteGrid
        />
      ) : null}
      {options.axes ? <axesHelper args={[radius * 1.4]} /> : null}
      {options.bounds && !box.isEmpty() ? <box3Helper args={[box, BOUNDS_COLOR]} /> : null}
      {selectedBox && !selectedBox.isEmpty() ? (
        <box3Helper args={[selectedBox, SELECTION_COLOR]} />
      ) : null}
      <OrbitControls makeDefault enableDamping={false} />
      {children}
    </>
  );
}

/** Interactive 3D view of a loaded model: orbit (drag), pan (right-drag), zoom (wheel/pinch). */
export function Viewport({ label, className, ...scene }: ViewportProps) {
  return (
    <Canvas
      className={className}
      role="img"
      aria-label={label}
      shadows
      frameloop="demand"
      dpr={[1, 2]}
      camera={{ fov: 40, near: 0.01, far: 1000, position: [2, 1.5, 2.5] }}
      gl={{ antialias: true }}
      onPointerMissed={(event) => {
        if (event.type === 'click') scene.onSelect(null);
      }}
    >
      <Scene {...scene} />
    </Canvas>
  );
}
