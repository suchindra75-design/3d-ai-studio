import * as THREE from 'three';

import { findProblemEdges } from './problems';

function triangles(points: number[][]): THREE.Mesh {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(points.flat(), 3));
  return new THREE.Mesh(geometry);
}

describe('problem edges', () => {
  it('finds nothing on a closed box, even with texture seams', () => {
    const box = new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1)); // 24 vertices: seams everywhere
    const edges = findProblemEdges(new THREE.Group().add(box));
    expect(edges.boundaryCount).toBe(0);
    expect(edges.nonManifoldCount).toBe(0);
  });

  it('finds the rim of a hole', () => {
    const geometry = new THREE.BoxGeometry(1, 1, 1).toNonIndexed();
    const position = geometry.getAttribute('position');
    // Drop the first face (two triangles): its four sides become open edges.
    const kept = new THREE.BufferGeometry();
    kept.setAttribute(
      'position',
      new THREE.Float32BufferAttribute(Array.from(position.array as Float32Array).slice(18), 3),
    );
    const edges = findProblemEdges(new THREE.Group().add(new THREE.Mesh(kept)));
    expect(edges.boundaryCount).toBe(4);
    expect(edges.boundary).toHaveLength(4 * 6);
  });

  it('finds an edge shared by three triangles, in world space', () => {
    const fin = triangles([
      [0, 0, 0],
      [1, 0, 0],
      [0, 1, 0],
      [1, 0, 0],
      [0, 0, 0],
      [0, -1, 0],
      [0, 0, 0],
      [1, 0, 0],
      [0, 0, 1],
    ]);
    fin.position.set(10, 0, 0);
    const edges = findProblemEdges(new THREE.Group().add(fin));
    expect(edges.nonManifoldCount).toBe(1);
    expect(Math.min(edges.nonManifold[0]!, edges.nonManifold[3]!)).toBe(10);
    expect(edges.boundaryCount).toBe(6); // the fins' outer edges
  });
});
