'use client';

import { Grid, OrbitControls } from '@react-three/drei';
import { Canvas, useFrame, useThree, type ThreeEvent } from '@react-three/fiber';
import { useEffect, useMemo, useRef, type MutableRefObject, type ReactNode } from 'react';
import * as THREE from 'three';
import { RoomEnvironment } from 'three/examples/jsm/environments/RoomEnvironment.js';

import { frameBox, gridCellSize, type CameraPreset } from './camera';
import { applyDisplayMode, type DisplayMode } from './display';
import { measure } from './inspect';
import { ModelLod, chooseLevel, projectedArea, type DetailMode, type LodSource } from './lod';

export interface ViewRequest {
  preset: CameraPreset;
  /** Change it to re-apply the same preset. */
  nonce: number;
}

/** Renders the view to a PNG at ``scale`` × its resolution (1 when omitted). */
export type CaptureFunction = (scale?: number) => Promise<Blob>;

/** studio: image lighting plus a key light; soft: brighter fill, weaker key; flat: no key light. */
export type LightingPreset = 'studio' | 'soft' | 'flat';

export const LIGHTING_PRESETS: { value: LightingPreset; label: string; hint: string }[] = [
  { value: 'studio', label: 'Studio', hint: 'Room lighting with a key light and shadows' },
  { value: 'soft', label: 'Soft', hint: 'Even light, faint shadows' },
  { value: 'flat', label: 'Flat', hint: 'No shading from a light: judge colours and textures' },
];

const LIGHTING: Record<LightingPreset, { environment: number; key: number; hemisphere: number }> = {
  studio: { environment: 0.85, key: 1.4, hemisphere: 0.35 },
  soft: { environment: 1.15, key: 0.55, hemisphere: 0.6 },
  flat: { environment: 1.5, key: 0, hemisphere: 0.9 },
};

/** Shares one camera between viewports (version comparison). */
export class CameraLink {
  position = new THREE.Vector3();
  target = new THREE.Vector3();
  /** Who moved last, so a viewport does not react to its own change. */
  source: object | null = null;
  private readonly listeners = new Set<() => void>();
  set(source: object, position: THREE.Vector3, target: THREE.Vector3): void {
    this.source = source;
    this.position.copy(position);
    this.target.copy(target);
    for (const listener of this.listeners) listener();
  }
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

export interface ViewportOptions {
  display: DisplayMode;
  lighting: LightingPreset;
  grid: boolean;
  axes: boolean;
  bounds: boolean;
  shadows: boolean;
}

export const DEFAULT_VIEWPORT_OPTIONS: ViewportOptions = {
  display: 'material',
  lighting: 'studio',
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
  /** 'measure': clicks on the model report the surface point instead of selecting. */
  tool?: 'select' | 'measure';
  onPick?: (point: THREE.Vector3) => void;
  /** View-only section: hides everything on the side the plane's normal points away from. */
  clipPlane?: THREE.Plane | null;
  /** Keeps this viewport's camera in step with others sharing the link. */
  cameraLink?: CameraLink;
  /** Receives a function that renders the current view to a PNG. */
  onCaptureReady?: (capture: CaptureFunction) => void;
  /** Simplified versions of ``model`` for drawing it when full detail is not visible. */
  lod?: { fullTriangles: number; sources: LodSource[] } | null;
  /** 'auto' (by on-screen size) or a fixed level; 0 = full detail. */
  detail?: DetailMode;
  /** Called when the level drawn changes. */
  onDetailLevel?: (level: number) => void;
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
function RoomLighting({ intensity }: { intensity: number }) {
  const get = useThree((state) => state.get);
  useEffect(() => {
    const { gl, scene, invalidate } = get();
    const pmrem = new THREE.PMREMGenerator(gl);
    const room = new RoomEnvironment();
    const target = pmrem.fromScene(room, 0.04);
    scene.environment = target.texture;
    invalidate();
    return () => {
      scene.environment = null;
      target.dispose();
      room.dispose();
      pmrem.dispose();
    };
  }, [get]);
  useEffect(() => {
    const { scene, invalidate } = get();
    scene.environmentIntensity = intensity;
    invalidate();
  }, [get, intensity]);
  return null;
}

/** Screenshots never exceed this many pixels on a side (and the GPU's own limits). */
export const MAX_CAPTURE_SIDE = 8192;

/**
 * Renders the current view to a PNG on request, at ``scale`` × the view's own
 * resolution (capped by MAX_CAPTURE_SIDE and the GPU). The canvas's on-screen
 * size never changes; its drawing buffer is resized for one frame and restored.
 */
function Capture({
  onReady,
  lod,
}: {
  onReady: (capture: CaptureFunction) => void;
  lod: MutableRefObject<ModelLod | null>;
}) {
  const get = useThree((state) => state.get);
  useEffect(() => {
    onReady(
      (scale = 1) =>
        new Promise<Blob>((resolve, reject) => {
          const { gl, scene, camera, size, invalidate } = get();
          const context = gl.getContext();
          const limit = Math.min(
            MAX_CAPTURE_SIDE,
            context.getParameter(context.MAX_RENDERBUFFER_SIZE) as number,
            ...(context.getParameter(context.MAX_VIEWPORT_DIMS) as number[]),
          );
          const ratio = gl.getPixelRatio();
          const wanted = ratio * Math.max(1, scale);
          const capped = Math.min(wanted, limit / Math.max(size.width, size.height, 1));
          gl.setPixelRatio(capped);
          // Screenshots always show full detail.
          const level = lod.current?.level ?? 0;
          lod.current?.update(0);
          try {
            gl.render(scene, camera);
            gl.domElement.toBlob((blob) =>
              blob ? resolve(blob) : reject(new Error('The view could not be captured.')),
            );
          } finally {
            gl.setPixelRatio(ratio);
            lod.current?.update(level);
            invalidate();
          }
        }),
    );
  }, [get, onReady, lod]);
  return null;
}

/** Copies camera moves to and from a shared link. */
function LinkedCamera({ link }: { link: CameraLink }) {
  const get = useThree((state) => state.get);
  const controls = useThree((state) => state.controls) as unknown as
    | (THREE.EventDispatcher<{ change: object }> & { target: THREE.Vector3; update: () => void })
    | null;
  useEffect(() => {
    if (!controls) return undefined;
    const me = {};
    let applying = false; // our own update() also fires 'change'
    const push = () => {
      if (!applying) link.set(me, get().camera.position, controls.target);
    };
    const pull = () => {
      if (link.source === me) return;
      applying = true;
      get().camera.position.copy(link.position);
      controls.target.copy(link.target);
      controls.update();
      applying = false;
      get().invalidate();
    };
    controls.addEventListener('change', push);
    const stop = link.subscribe(pull);
    return () => {
      controls.removeEventListener('change', push);
      stop();
    };
  }, [controls, get, link]);
  return null;
}

/** Section view: renderer clipping, so nothing in the model changes. */
function Section({ plane }: { plane: THREE.Plane }) {
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
  return null;
}

/** Draws simplified copies of the model when full detail would not be visible. */
function LodLayer({
  model,
  lod,
  detail,
  box,
  onLevel,
  handleRef,
}: {
  model: THREE.Object3D;
  lod: { fullTriangles: number; sources: LodSource[] };
  detail: DetailMode;
  box: THREE.Box3;
  onLevel?: (level: number) => void;
  handleRef: MutableRefObject<ModelLod | null>;
}) {
  const raycaster = useThree((state) => state.raycaster);
  const invalidate = useThree((state) => state.invalidate);
  // Creating it has no side effects (copies share the loaded geometry); only the
  // effect below changes the scene, and undoing that (dispose) can be repeated.
  const modelLod = useMemo(() => new ModelLod(model, lod.fullTriangles, lod.sources), [model, lod]);
  useEffect(() => {
    // Hidden full parts stay pickable: they are what is measured and selected.
    raycaster.layers.enableAll();
    handleRef.current = modelLod;
    invalidate();
    return () => {
      handleRef.current = null;
      modelLod.dispose();
      invalidate();
    };
  }, [modelLod, raycaster, handleRef, invalidate]);
  useEffect(() => invalidate(), [detail, invalidate]);
  const reported = useRef(-1);
  useFrame((state) => {
    let level = typeof detail === 'number' ? detail : 0;
    if (detail === 'auto' && !box.isEmpty()) {
      const sphere = box.getBoundingSphere(new THREE.Sphere());
      const area = projectedArea(
        sphere.center,
        sphere.radius,
        state.camera,
        state.size.height * state.gl.getPixelRatio(),
      );
      level = chooseLevel(modelLod.triangles, area, modelLod.level);
    }
    modelLod.update(level);
    if (modelLod.level !== reported.current) {
      reported.current = modelLod.level;
      onLevel?.(modelLod.level);
    }
  });
  return <primitive object={modelLod.group} />;
}

function KeyLight({
  box,
  shadows,
  intensity,
}: {
  box: THREE.Box3;
  shadows: boolean;
  intensity: number;
}) {
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
      castShadow={shadows && intensity > 0}
      intensity={intensity}
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
  tool = 'select',
  onPick,
  clipPlane,
  cameraLink,
  onCaptureReady,
  lod,
  detail = 'auto',
  onDetailLevel,
}: Omit<ViewportProps, 'label' | 'className'>) {
  const lodHandle = useRef<ModelLod | null>(null);
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

  const light = LIGHTING[options.lighting];
  const radius = Math.max(box.getBoundingSphere(new THREE.Sphere()).radius, 1e-3);
  const floor = box.isEmpty() ? 0 : box.min.y;
  const center = box.getCenter(new THREE.Vector3());
  const cell = gridCellSize(box);

  function handleClick(event: ThreeEvent<MouseEvent>) {
    if (event.delta > CLICK_TOLERANCE_PX) return;
    event.stopPropagation();
    if (tool === 'measure') onPick?.(event.point.clone());
    else onSelect(event.object.uuid);
  }

  return (
    <>
      <RoomLighting intensity={light.environment} />
      <hemisphereLight args={['#ffffff', '#3a3a40', light.hemisphere]} />
      <KeyLight box={box} shadows={options.shadows} intensity={light.key} />
      {clipPlane ? <Section plane={clipPlane} /> : null}
      {cameraLink ? <LinkedCamera link={cameraLink} /> : null}
      {onCaptureReady ? <Capture onReady={onCaptureReady} lod={lodHandle} /> : null}
      <primitive object={model} onClick={handleClick} />
      {lod && lod.sources.length > 0 ? (
        <LodLayer
          model={model}
          lod={lod}
          detail={detail}
          box={box}
          onLevel={onDetailLevel}
          handleRef={lodHandle}
        />
      ) : null}
      {options.shadows && light.key > 0 ? (
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
