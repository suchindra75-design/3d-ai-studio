/** Client-side pre-checks. The backend re-validates everything by content. */

export const ACCEPTED_IMAGE_TYPES = ['image/jpeg', 'image/png', 'image/webp'] as const;
export const ACCEPTED_IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.webp'] as const;
export const IMAGE_ACCEPT_ATTRIBUTE = [...ACCEPTED_IMAGE_TYPES, ...ACCEPTED_IMAGE_EXTENSIONS].join(
  ',',
);

export interface FileCheck {
  file: File;
  ok: boolean;
  reason?: string;
}

function hasAcceptedExtension(name: string): boolean {
  const lower = name.toLowerCase();
  return ACCEPTED_IMAGE_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

export function checkImageFile(file: File, maxBytes: number): FileCheck {
  const typeOk =
    (ACCEPTED_IMAGE_TYPES as readonly string[]).includes(file.type) ||
    (file.type === '' && hasAcceptedExtension(file.name));
  if (!typeOk) {
    return { file, ok: false, reason: 'Only JPEG, PNG and WebP images are supported.' };
  }
  if (file.size === 0) return { file, ok: false, reason: 'The file is empty.' };
  if (file.size > maxBytes) {
    const mb = Math.round(maxBytes / (1024 * 1024));
    return { file, ok: false, reason: `The file is larger than the ${mb} MB limit.` };
  }
  return { file, ok: true };
}
