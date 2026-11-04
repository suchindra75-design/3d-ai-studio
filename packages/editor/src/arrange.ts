/**
 * Structural edits: delete a part, place the model on the ground or at the
 * origin, mirror, and move a part's pivot. Each returns an undoable command
 * (not yet applied; run it with `history.execute`).
 */
import { displayName, measure } from '@ai3d/viewer';
import * as THREE from 'three';

import { applyTransform, captureTransform, sameTransform, type TransformState } from './commands';
import type { Command } from './history';

/** An edit that cannot be done, with a message for the person. */
export class EditError extends Error {
  override name = 'EditError';
}

export type Axis = 'x' | 'y' | 'z';

const AXES: Record<Axis, THREE.Vector3> = {
  x: new THREE.Vector3(1, 0, 0),
  y: new THREE.Vector3(0, 1, 0),
  z: new THREE.Vector3(0, 0, 1),
};

export function axisVector(axis: Axis): THREE.Vector3 {
  return AXES[axis].clone();
}

/** The objects a whole-model edit moves: the file's root nodes (see exportGlb). */
function modelNodes(root: THREE.Object3D): THREE.Object3D[] {
  return [...root.children];
}

/**
 * Applies ``world`` (a world-space transform) to each object, keeping it under
 * its parent, and returns the command that does it. Throws when the result
 * cannot be written as position, rotation and scale (a skewed parent).
 */
function worldTransformCommand(
  objects: THREE.Object3D[],
  world: THREE.Matrix4,
  label: string,
): Command {
  const before: TransformState[] = objects.map(captureTransform);
  const after: TransformState[] = objects.map((object) => {
    object.updateWorldMatrix(true, false);
    object.updateMatrix();
    const parent = object.parent?.matrixWorld ?? new THREE.Matrix4();
    const local = parent.clone().invert().multiply(world).multiply(parent).multiply(object.matrix);
    const position = new THREE.Vector3();
    const quaternion = new THREE.Quaternion();
    const scale = new THREE.Vector3();
    local.decompose(position, quaternion, scale);
    const check = new THREE.Matrix4().compose(position, quaternion, scale);
    const size = Math.max(1, ...local.elements.map(Math.abs));
    if (check.elements.some((value, i) => Math.abs(value - local.elements[i]!) > 1e-6 * size)) {
      throw new EditError(
        `"${displayName(object)}" sits under a skewed parent, so this edit cannot be stored exactly.`,
      );
    }
    return {
      position: position.toArray(),
      quaternion: quaternion.toArray() as TransformState['quaternion'],
      scale: scale.toArray(),
    };
  });
  if (before.every((state, i) => sameTransform(state, after[i]!))) {
    throw new EditError('Nothing to change: the model is already there.');
  }
  return {
    label,
    apply: () => objects.forEach((object, i) => applyTransform(object, after[i]!)),
    revert: () => objects.forEach((object, i) => applyTransform(object, before[i]!)),
  };
}

function boundsOf(object: THREE.Object3D): THREE.Box3 {
  const box = measure(object);
  if (box.isEmpty()) throw new EditError('The model has no geometry to measure.');
  return box;
}

/** Moves the whole model up or down so its lowest point is at height 0. */
export function placeOnGroundCommand(root: THREE.Object3D): Command {
  const box = boundsOf(root);
  const move = new THREE.Matrix4().makeTranslation(0, -box.min.y, 0);
  return worldTransformCommand(modelNodes(root), move, 'Placed the model on the ground');
}

/** Moves the whole model so it stands on the ground, centred on the origin. */
export function centreOnOriginCommand(root: THREE.Object3D): Command {
  const box = boundsOf(root);
  const centre = box.getCenter(new THREE.Vector3());
  const move = new THREE.Matrix4().makeTranslation(-centre.x, -box.min.y, -centre.z);
  return worldTransformCommand(modelNodes(root), move, 'Centred the model on the origin');
}

/**
 * Mirrors ``target`` across the plane through its centre, perpendicular to
 * ``axis`` (world axes). ``target`` may be the model root (the whole model).
 * Stored as a negative scale, which glTF readers turn back into correct faces.
 */
export function mirrorCommand(root: THREE.Object3D, target: THREE.Object3D, axis: Axis): Command {
  const whole = target === root;
  const centre = boundsOf(target).getCenter(new THREE.Vector3());
  const flip = new THREE.Vector3(1, 1, 1).setComponent(['x', 'y', 'z'].indexOf(axis), -1);
  const world = new THREE.Matrix4()
    .makeTranslation(centre.x, centre.y, centre.z)
    .multiply(new THREE.Matrix4().makeScale(flip.x, flip.y, flip.z))
    .multiply(new THREE.Matrix4().makeTranslation(-centre.x, -centre.y, -centre.z));
  const name = whole ? 'the model' : `"${displayName(target)}"`;
  return worldTransformCommand(
    whole ? modelNodes(root) : [target],
    world,
    `Mirrored ${name} along ${axis.toUpperCase()}`,
  );
}

/** Removes a part (and everything under it). Undo puts it back in the same place. */
export function deleteCommand(object: THREE.Object3D): Command {
  const parent = object.parent;
  if (!parent) throw new EditError('The whole model cannot be deleted here.');
  const index = parent.children.indexOf(object);
  return {
    label: `Deleted "${displayName(object)}"`,
    apply: () => {
      parent.remove(object);
    },
    revert: () => {
      parent.add(object);
      parent.children.pop();
      parent.children.splice(index, 0, object);
      object.updateMatrixWorld(true);
    },
  };
}

/**
 * Moves a part's pivot (its origin, which rotate and scale turn around) to the
 * centre of its bounds without moving anything on screen. The part's own
 * geometry is shifted on a copy, so other parts sharing it are not affected.
 */
export function pivotToCentreCommand(object: THREE.Object3D): Command {
  const mesh = object as THREE.Mesh;
  if (
    (object as THREE.SkinnedMesh).isSkinnedMesh ||
    (object as THREE.InstancedMesh).isInstancedMesh
  ) {
    throw new EditError('The pivot of skinned or instanced parts cannot be moved here.');
  }
  const centre = boundsOf(object).getCenter(new THREE.Vector3());
  object.updateWorldMatrix(true, false);
  const offset = object.worldToLocal(centre.clone()); // the new pivot, in the part's own space
  if (offset.lengthSq() < 1e-18) throw new EditError('The pivot is already at the centre.');

  object.updateMatrix();
  const position = offset.clone().applyMatrix4(object.matrix);
  const before = object.position.clone();
  const geometry = mesh.isMesh ? mesh.geometry : null;
  const moved = geometry?.clone().translate(-offset.x, -offset.y, -offset.z) ?? null;
  const children = [...object.children];
  const shift = (sign: number) => {
    for (const child of children) child.position.addScaledVector(offset, sign);
  };
  return {
    label: `Moved the pivot of "${displayName(object)}" to its centre`,
    apply: () => {
      object.position.copy(position);
      if (moved) mesh.geometry = moved;
      shift(-1);
      object.updateMatrixWorld(true);
    },
    revert: () => {
      object.position.copy(before);
      if (geometry) mesh.geometry = geometry;
      shift(1);
      object.updateMatrixWorld(true);
    },
  };
}
