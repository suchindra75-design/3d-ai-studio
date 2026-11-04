'use client';

import { Button, cn, type ButtonProps } from '@ai3d/ui';
import { Download } from 'lucide-react';
import { useState, type ReactNode } from 'react';

import { useStudio } from '../context';

/** Same file name rule as the backend's downloads: `<model>-v<n>.<ext>`. */
export function versionFileName(modelName: string, versionNumber: number, extension: string) {
  const slug =
    modelName
      .replace(/[^A-Za-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .toLowerCase()
      .slice(0, 80) || 'model';
  return `${slug}-v${versionNumber}.${extension}`;
}

/**
 * Downloads a backend file. In a browser it is a plain download link; in the
 * desktop app (whose web view cannot download) it opens the native Save dialog.
 */
export function DownloadButton({
  path,
  fileName,
  label,
  children,
  size = 'sm',
  variant = 'secondary',
  className,
}: {
  path: string;
  fileName: string;
  label: string;
  children?: ReactNode;
  size?: ButtonProps['size'];
  variant?: ButtonProps['variant'];
  className?: string;
}) {
  const { api, desktop } = useStudio();
  const [state, setState] = useState<{ busy?: boolean; saved?: string; error?: string }>({});
  const content = children ?? (
    <>
      <Download aria-hidden /> Download
    </>
  );
  if (!desktop) {
    return (
      <Button asChild size={size} variant={variant} className={className}>
        <a href={api.url(path)} download={fileName} aria-label={label}>
          {content}
        </a>
      </Button>
    );
  }
  async function save() {
    setState({ busy: true });
    try {
      const saved = await desktop!.saveFile(path, fileName);
      setState(saved ? { saved } : {});
    } catch (error) {
      setState({ error: error instanceof Error ? error.message : String(error) });
    }
  }
  return (
    <span className={cn('inline-flex flex-col gap-1', className)}>
      <Button size={size} variant={variant} aria-label={label} disabled={state.busy} onClick={save}>
        {content}
      </Button>
      {state.saved ? (
        <span
          role="status"
          className="max-w-56 truncate text-[11px] text-success"
          title={state.saved}
        >
          Saved to {state.saved}
        </span>
      ) : null}
      {state.error ? (
        <span role="alert" className="text-[11px] text-danger">
          {state.error}
        </span>
      ) : null}
    </span>
  );
}
