import { applyDisplayMode, parseGlb } from '@ai3d/viewer';
import * as THREE from 'three';

import {
  captureTransform,
  colorCommand,
  colorHex,
  describeTransform,
  scalarCommand,
  transformCommand,
} from './commands';
import { exportGlb } from './export';
import { EditHistory, type Command } from './history';

function counter(log: string[], label: string): Command {
  return { label, apply: () => log.push(`do ${label}`), revert: () => log.push(`undo ${label}`) };
}

describe('EditHistory', () => {
  it('undoes and redoes in order and drops the redo stack on a new edit', () => {
    const log: string[] = [];
    const history = new EditHistory();
    history.execute(counter(log, 'a'));
    history.execute(counter(log, 'b'));
    expect(history.undoLabel).toBe('b');
    history.undo();
    expect(history.redoLabel).toBe('b');
    history.redo();
    history.undo();
    history.execute(counter(log, 'c'));
    expect(history.canRedo).toBe(false);
    expect(log).toEqual(['do a', 'do b', 'undo b', 'do b', 'undo b', 'do c']);
    expect(history.changes()).toEqual(['a', 'c']);
  });

  it('is clean again after undoing everything, and dirty after recorded edits', () => {
    const history = new EditHistory();
    expect(history.dirty).toBe(false);
    history.record(counter([], 'moved'));
    expect(history.dirty).toBe(true);
    history.undo();
    expect(history.dirty).toBe(false);
    expect(history.undo()).toBeUndefined();
  });

  it('folds repeated edits and remembers edits beyond the limit', () => {
    const history = new EditHistory(3);
    for (let i = 0; i < 5; i += 1) history.record(counter([], 'Moved "Cup"'));
    history.record(counter([], 'Scaled "Cup"'));
    expect(history.changes()).toEqual(['3 earlier edits', 'Moved "Cup" (×2)', 'Scaled "Cup"']);
    history.undo();
    history.undo();
    history.undo();
    // The dropped edits cannot be undone, so the model still differs from its version.
    expect(history.canUndo).toBe(false);
    expect(history.dirty).toBe(true);
    history.clear();
    expect(history.dirty).toBe(false);
  });

  it('notifies subscribers with a new version', () => {
    const history = new EditHistory();
    const listener = vi.fn();
    const unsubscribe = history.subscribe(listener);
    history.execute(counter([], 'x'));
    expect(listener).toHaveBeenCalledTimes(1);
    expect(history.getVersion()).toBe(1);
    unsubscribe();
    history.undo();
    expect(listener).toHaveBeenCalledTimes(1);
  });
});

describe('transform commands', () => {
  it('applies and reverts a recorded drag', () => {
    const object = new THREE.Object3D();
    const before = captureTransform(object);
    object.position.set(1, 2, 3);
    object.scale.setScalar(2);
    const after = captureTransform(object);
    const history = new EditHistory();
    history.record(
      transformCommand(object, before, after, describeTransform('Cup', before, after)),
    );
    expect(history.undoLabel).toBe('Transformed "Cup"');
    history.undo();
    expect(object.position.toArray()).toEqual([0, 0, 0]);
    expect(object.scale.toArray()).toEqual([1, 1, 1]);
    history.redo();
    expect(object.position.toArray()).toEqual([1, 2, 3]);
  });

  it('names a single kind of change', () => {
    const object = new THREE.Object3D();
    const before = captureTransform(object);
    object.rotation.y = 1;
    expect(describeTransform('Lid', before, captureTransform(object))).toBe('Rotated "Lid"');
  });
});

describe('material commands', () => {
  it('edits colour as sRGB hex and restores the exact previous colour', () => {
    const material = new THREE.MeshStandardMaterial({ name: 'Glaze', color: 0x336699 });
    const history = new EditHistory();
    history.execute(colorCommand(material, '#FF8800'));
    expect(colorHex(material)).toBe('#ff8800');
    expect(history.undoLabel).toBe('Changed colour of "Glaze" to #ff8800');
    history.undo();
    expect(colorHex(material)).toBe('#336699');
  });

  it('clamps scalars and enables blending only while opacity is below 1', () => {
    const material = new THREE.MeshStandardMaterial({ name: 'Glass' });
    const history = new EditHistory();
    history.execute(scalarCommand(material, 'roughness', 1.7));
    expect(material.roughness).toBe(1);
    history.execute(scalarCommand(material, 'opacity', 0.4));
    expect(material.transparent).toBe(true);
    expect(history.undoLabel).toBe('Set opacity of "Glass" to 0.40');
    history.undo();
    expect(material.opacity).toBe(1);
    expect(material.transparent).toBe(false);
  });
});

describe('exportGlb', () => {
  it('round-trips edits with the real materials, whatever the view shows', async () => {
    const material = new THREE.MeshStandardMaterial({ name: 'Glaze', color: 0x336699 });
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), material);
    mesh.name = 'Body';
    const hidden = new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), material);
    hidden.name = 'Hidden';
    hidden.visible = false;
    const root = new THREE.Group();
    root.name = 'Model';
    root.add(mesh, hidden);

    const history = new EditHistory();
    history.execute(colorCommand(material, '#ff8800'));
    const before = captureTransform(mesh);
    mesh.scale.set(2, 1, 1);
    history.record(transformCommand(mesh, before, captureTransform(mesh), 'Scaled "Body"'));
    applyDisplayMode(root, 'wireframe');

    const glb = await exportGlb(root);
    expect(new TextDecoder().decode(glb.slice(0, 4))).toBe('glTF');

    const { root: reopened } = await parseGlb(glb);
    const body = reopened.getObjectByName('Body') as THREE.Mesh;
    expect(body.scale.toArray()).toEqual([2, 1, 1]);
    const saved = body.material as THREE.MeshStandardMaterial;
    expect(saved.name).toBe('Glaze');
    expect(saved.wireframe).toBe(false);
    expect(colorHex(saved)).toBe('#ff8800');
    // Kept, and visible again: glTF files have no visibility flag.
    expect(reopened.getObjectByName('Hidden')?.visible).toBe(true);
    expect(reopened.name).toBe('Model');
    expect(reopened.children.map((c) => c.name)).toEqual(['Body', 'Hidden']);

    // Saving again keeps the same hierarchy (no wrapper node per save).
    const again = await parseGlb(await exportGlb(reopened));
    expect(again.root.children.map((c) => c.name)).toEqual(['Body', 'Hidden']);
  });
});
