import { IMAGE_ACCEPT_ATTRIBUTE, checkImageFile } from './files';

const MB = 1024 * 1024;
const file = (name: string, type: string, size = 10) =>
  new File([new Uint8Array(size)], name, { type });

describe('checkImageFile', () => {
  it('accepts supported image types', () => {
    for (const [name, type] of [
      ['a.jpg', 'image/jpeg'],
      ['a.png', 'image/png'],
      ['a.webp', 'image/webp'],
    ]) {
      expect(checkImageFile(file(name!, type!), MB).ok).toBe(true);
    }
  });

  it('falls back to the extension when the browser gives no type', () => {
    expect(checkImageFile(file('IMG_0001.JPG', ''), MB).ok).toBe(true);
    expect(checkImageFile(file('scan.tiff', ''), MB).ok).toBe(false);
  });

  it('rejects other types, empty files and oversize files', () => {
    expect(checkImageFile(file('a.gif', 'image/gif'), MB).reason).toMatch(/JPEG, PNG and WebP/);
    expect(checkImageFile(file('a.txt', 'text/plain'), MB).ok).toBe(false);
    expect(checkImageFile(file('a.jpg', 'image/jpeg', 0), MB).reason).toMatch(/empty/);
    expect(checkImageFile(file('a.jpg', 'image/jpeg', 2 * MB), MB).reason).toMatch(/1 MB limit/);
  });

  it('exposes an accept attribute for file inputs', () => {
    expect(IMAGE_ACCEPT_ATTRIBUTE).toContain('image/webp');
    expect(IMAGE_ACCEPT_ATTRIBUTE).toContain('.jpeg');
  });
});
