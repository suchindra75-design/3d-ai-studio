/**
 * Facts about a loaded scene: counts, hierarchy and measured size.
 *
 * Everything here reads real geometry; nothing is estimated. Sizes are in the
 * file's units (metres, per the glTF specification) — for AI-generated models
 * that scale is arbitrary, which the UI states next to the numbers.
 */
import * as THREE from 'three';

export interface SceneStats {
  meshes: number;
  vertices: number;
  triangles: number;
  materials: number;
  textures: number;
}

export type NodeKind = 'mesh' | 'group' | 'light' | 'camera' | 'other';

export interface HierarchyNode {
  id: string;
  name: string;
  kind: NodeKind;
  /** Triangles in this node and everything below it. */
  triangles: number;
  children: HierarchyNode[];
}

function isMesh(object: THREE.Object3D): object is THREE.Mesh {
  return (object as THREE.Mesh).isMesh === true;
}

/** Vertex and triangle counts of one mesh (instances included). */
export function meshCounts(mesh: THREE.Mesh): { vertices: number; triangles: number } {
  const geometry = mesh.geometry;
  const position = geometry.getAttribute('position');
  const vertices = position ? position.count : 0;
  const indexed = geometry.index ? geometry.index.count : vertices;
  const instances = (mesh as THREE.InstancedMesh).isInstancedMesh
    ? (mesh as THREE.InstancedMesh).count
    : 1;
  return { vertices: vertices * instances, triangles: Math.floor(indexed / 3) * instances };
}

export function materialsOf(mesh: THREE.Mesh): THREE.Material[] {
  return Array.isArray(mesh.material) ? mesh.material : [mesh.material];
}

function texturesOf(material: THREE.Material): THREE.Texture[] {
  return Object.values(material).filter(
    (value): value is THREE.Texture => value instanceof THREE.Texture,
  );
}

/**
 * Counts for a subtree. ``source`` maps a mesh to its own materials when a
 * display mode has swapped them (see display.ts).
 */
export function sceneStats(
  root: THREE.Object3D,
  source: (mesh: THREE.Mesh) => THREE.Material[] = materialsOf,
): SceneStats {
  const materials = new Set<THREE.Material>();
  const textures = new Set<THREE.Texture>();
  const stats: SceneStats = { meshes: 0, vertices: 0, triangles: 0, materials: 0, textures: 0 };
  root.traverse((object) => {
    if (!isMesh(object)) return;
    const counts = meshCounts(object);
    stats.meshes += 1;
    stats.vertices += counts.vertices;
    stats.triangles += counts.triangles;
    for (const material of source(object)) {
      materials.add(material);
      for (const texture of texturesOf(material)) textures.add(texture);
    }
  });
  stats.materials = materials.size;
  stats.textures = textures.size;
  return stats;
}

function kindOf(object: THREE.Object3D): NodeKind {
  if (isMesh(object)) return 'mesh';
  if ((object as THREE.Light).isLight) return 'light';
  if ((object as THREE.Camera).isCamera) return 'camera';
  if ((object as THREE.Group).isGroup || object.type === 'Object3D') return 'group';
  return 'other';
}

const KIND_LABEL: Record<NodeKind, string> = {
  mesh: 'Mesh',
  group: 'Group',
  light: 'Light',
  camera: 'Camera',
  other: 'Node',
};

export function displayName(object: THREE.Object3D): string {
  return object.name.trim() || KIND_LABEL[kindOf(object)];
}

/** The scene graph as plain data for the hierarchy panel. */
export function buildHierarchy(root: THREE.Object3D): HierarchyNode {
  const children = root.children.map(buildHierarchy);
  const own = isMesh(root) ? meshCounts(root).triangles : 0;
  return {
    id: root.uuid,
    name: displayName(root),
    kind: kindOf(root),
    triangles: own + children.reduce((sum, child) => sum + child.triangles, 0),
    children,
  };
}

/** Exact world-space bounds (every vertex), so rotated parts are not over-estimated. */
export function measure(object: THREE.Object3D): THREE.Box3 {
  object.updateWorldMatrix(true, true);
  return new THREE.Box3().setFromObject(object, true);
}

/** "12.3 cm" style lengths, choosing a unit that keeps three significant digits readable. */
export function formatLength(metres: number): string {
  const abs = Math.abs(metres);
  if (abs === 0) return '0 m';
  if (abs < 0.01) return `${(metres * 1000).toPrecision(3)} mm`;
  if (abs < 1) return `${(metres * 100).toPrecision(3)} cm`;
  if (abs < 1000) return `${metres.toPrecision(3)} m`;
  return `${(metres / 1000).toPrecision(3)} km`;
}

/** A length in a model whose real-world scale is unknown: three significant digits, no unit. */
export function formatUnits(value: number): string {
  if (value === 0) return '0 units';
  return `${Number(value.toPrecision(3))} units`;
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat('en-US').format(value);
}
