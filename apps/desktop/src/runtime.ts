import type { DesktopFeatures, RuntimeFact } from '@ai3d/studio';
import { invoke, isTauri } from '@tauri-apps/api/core';

interface AppInfo {
  version: string;
  tauri_version: string;
  os: string;
  arch: string;
}

/** The private backend as the Rust side reports it (see src-tauri/src/sidecar.rs). */
export type BackendView = {
  data_dir: string;
  log_path: string;
  program: string;
} & (
  | { state: 'starting' }
  | { state: 'running'; base_url: string; port: number; token: string }
  | { state: 'failed'; message: string; log_tail: string[] }
  | { state: 'stopped' }
);

export interface Connection {
  baseUrl: string;
  token?: string;
  backend?: Extract<BackendView, { state: 'running' }>;
}

export class BackendFailedError extends Error {
  constructor(readonly view: Extract<BackendView, { state: 'failed' }>) {
    super(view.message);
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Where the studio's API lives. In the app: the backend the shell started (waits
 * until it answers). In a plain browser (vite dev / e2e): VITE_AI3D_API_URL.
 */
export async function connectBackend(onStatus: (view: BackendView) => void): Promise<Connection> {
  if (!isTauri()) {
    return {
      baseUrl: import.meta.env.VITE_AI3D_API_URL ?? 'http://127.0.0.1:8000',
      token: import.meta.env.VITE_AI3D_API_TOKEN || undefined,
    };
  }
  for (;;) {
    const view = await invoke<BackendView>('backend_status');
    onStatus(view);
    if (view.state === 'running')
      return { baseUrl: view.base_url, token: view.token, backend: view };
    if (view.state === 'failed') throw new BackendFailedError(view);
    await sleep(400);
  }
}

export function restartBackend(): Promise<void> {
  return invoke('restart_backend');
}

/** Native features; undefined in a plain browser. */
export const desktopFeatures: DesktopFeatures | undefined = isTauri()
  ? {
      saveFile: (path, suggestedName) =>
        invoke<string | null>('save_backend_file', { path, suggestedName }),
      openDataFolder: () => invoke('open_data_folder'),
    }
  : undefined;

/** Facts shown on the About screen. */
export async function loadRuntimeFacts(connection: Connection): Promise<RuntimeFact[]> {
  if (!isTauri()) return [{ label: 'Shell', value: 'Browser preview (not running in Tauri)' }];
  const info = await invoke<AppInfo>('app_info');
  const facts: RuntimeFact[] = [
    { label: 'Desktop shell', value: `v${info.version} (Tauri ${info.tauri_version})` },
    { label: 'Platform', value: `${info.os} · ${info.arch}` },
  ];
  if (connection.backend) {
    facts.push(
      {
        label: 'Local engine',
        value: `Private backend on 127.0.0.1:${connection.backend.port} (per-launch token)`,
      },
      { label: 'Engine log', value: connection.backend.log_path },
    );
  }
  return facts;
}
