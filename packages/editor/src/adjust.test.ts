import * as THREE from 'three';

import {
  NO_ADJUST,
  adjustPixels,
  adjustRgb,
  colourAdjustCommand,
  colourTarget,
  meshesUsing,
  tilingCommand,
  transformUv,
} from './adjust';
import { EditError } from './arrange';
import { EditHistory } from './history';

const rgb = (r: number, g: number, b: number): [number, number, number] => [r, g, b];

describe('colour adjustments', () => {
  it('brightens, adds contrast, desaturates and turns hue', () => {
    const brighter = rgb(0.4, 0.4, 0.4);
    adjustRgb(brighter, { ...NO_ADJUST, brightness: 0.5 });
    expect(brighter[0]).toBeCloseTo(0.6, 6);

    const punchier = rgb(0.75, 0.25, 0.5);
    adjustRgb(punchier, { ...NO_ADJUST, contrast: 1 });
    expect(punchier).toEqual([1, 0, 0.5]); // clamped, mid-grey unchanged

    const grey = rgb(0.9, 0.2, 0.1);
    adjustRgb(grey, { ...NO_ADJUST, saturation: -1 });
    expect(grey[0]).toBeCloseTo(grey[1], 6);
    expect(grey[1]).toBeCloseTo(grey[2], 6);

    const red = rgb(1, 0, 0);
    adjustRgb(red, { ...NO_ADJUST, hue: 120 });
    expect(red[1]).toBeGreaterThan(0.9); // red → green
    expect(red[0]).toBeLessThan(0.1);
    const neutral = rgb(0.3, 0.3, 0.3);
    adjustRgb(neutral, { ...NO_ADJUST, hue: 77 });
    expect(neutral.map((c) => Number(c.toFixed(6)))).toEqual([0.3, 0.3, 0.3]);
  });

  it('adjusts pixel bytes and keeps alpha', () => {
    const data = new Uint8ClampedArray([100, 100, 100, 7, 255, 0, 0, 255]);
    adjustPixels(data, { ...NO_ADJUST, brightness: -0.5 });
    expect(Array.from(data)).toEqual([50, 50, 50, 7, 128, 0, 0, 255]);
  });

  it('adjusts vertex colours (in sRGB) when there is no texture, undoably', () => {
    const geometry = new THREE.BoxGeometry();
    const linear = new THREE.Color(0.5, 0.5, 0.5).convertSRGBToLinear();
    geometry.setAttribute(
      'color',
      new THREE.Float32BufferAttribute(
        Array.from({ length: geometry.getAttribute('position').count }, () => [
          linear.r,
          linear.g,
          linear.b,
        ]).flat(),
        3,
      ),
    );
    const material = new THREE.MeshStandardMaterial({ name: 'Plush', vertexColors: true });
    const mesh = new THREE.Mesh(geometry, material);
    const root = new THREE.Group().add(mesh);
    const meshes = meshesUsing(root, material, (m) => [m.material as THREE.Material]);
    expect(colourTarget(material, meshes)).toBe('vertex');
    const history = new EditHistory();
    history.execute(colourAdjustCommand(material, meshes, { ...NO_ADJUST, brightness: 0.5 }));
    const after = new THREE.Color().setRGB(
      geometry.getAttribute('color').getX(0),
      0,
      0,
      THREE.LinearSRGBColorSpace,
    );
    expect(after.getStyle(THREE.SRGBColorSpace)).toMatch(/^rgb\(19[01],/); // 0.5 × 1.5 = 0.75 sRGB
    expect(history.changes()[0]).toBe('Adjusted the colours of "Plush" (brightness +0.50)');
    history.undo();
    expect(geometry.getAttribute('color').getX(0)).toBeCloseTo(linear.r, 6);
    expect(() => colourAdjustCommand(material, meshes, NO_ADJUST)).toThrow(EditError);
    const plain = new THREE.MeshStandardMaterial();
    expect(() =>
      colourAdjustCommand(plain, [new THREE.Mesh(new THREE.BoxGeometry(), plain)], {
        ...NO_ADJUST,
        hue: 10,
      }),
    ).toThrow(/no texture or vertex colours/);
  });
});

describe('tiling', () => {
  it('scales texture coordinates around the centre, then offsets and turns them', () => {
    expect(transformUv(0.5, 0.5, { repeat: [3, 3], offset: [0, 0], rotation: 0 })).toEqual([
      0.5, 0.5,
    ]);
    expect(transformUv(1, 1, { repeat: [2, 4], offset: [0.1, 0], rotation: 0 })).toEqual([
      1.6, 2.5,
    ]);
    const [u, v] = transformUv(1, 0.5, { repeat: [1, 1], offset: [0, 0], rotation: 90 });
    expect(u).toBeCloseTo(0.5, 9);
    expect(v).toBeCloseTo(1, 9);
  });

  it('bakes tiling into a copy of each part’s geometry and makes the texture repeat', () => {
    const geometry = new THREE.PlaneGeometry(1, 1);
    const texture = new THREE.Texture();
    texture.wrapS = THREE.ClampToEdgeWrapping;
    const material = new THREE.MeshStandardMaterial({ name: 'Floor', map: texture });
    const mesh = new THREE.Mesh(geometry, material);
    const twin = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial());
    const history = new EditHistory();
    history.execute(
      tilingCommand(material, [mesh], { repeat: [4, 4], offset: [0, 0], rotation: 0 }),
    );
    const uv = mesh.geometry.getAttribute('uv');
    const us = Array.from({ length: uv.count }, (_, i) => uv.getX(i));
    expect(Math.max(...us) - Math.min(...us)).toBeCloseTo(4, 6);
    expect(texture.wrapS).toBe(THREE.RepeatWrapping);
    expect(twin.geometry).toBe(geometry); // the shared original is untouched
    expect(history.changes()[0]).toBe('Tiled the texture of "Floor" (4 × 4)');
    history.undo();
    expect(mesh.geometry).toBe(geometry);
    expect(texture.wrapS).toBe(THREE.ClampToEdgeWrapping);
    expect(() =>
      tilingCommand(material, [mesh], { repeat: [1, 1], offset: [0, 0], rotation: 0 }),
    ).toThrow(/Nothing to change/);
    expect(() =>
      tilingCommand(material, [mesh], { repeat: [0, 1], offset: [0, 0], rotation: 0 }),
    ).toThrow(/more than 0/);
  });
});
