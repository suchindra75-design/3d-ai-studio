'use client';

import type { LocalDeviceRead } from '@ai3d/shared-types';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Skeleton,
} from '@ai3d/ui';
import { formatBytes, formatRelativeTime } from '@ai3d/utilities';
import { CircleCheck, CircleX, FolderOpen, RotateCw } from 'lucide-react';
import type { ReactNode } from 'react';

import { useLocalDevice, useSystemInfo } from '../api/hooks';
import { ErrorState, PageContainer, PageHeader } from '../components/page';
import { ProviderSettings } from '../components/provider-settings';
import { useStudio } from '../context';

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)] gap-4 py-2 text-[13px]">
      <dt className="text-subtle-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-foreground">{children}</dd>
    </div>
  );
}

const CAPABILITY_LABELS: Record<string, string> = {
  local_ai_generation: 'Local AI generation',
  photogrammetry_sparse: 'Photogrammetry (camera poses)',
  photogrammetry_dense: 'Photogrammetry (dense surfaces)',
  disk_space: 'Disk space',
};

function DeviceDetails({ device }: { device: LocalDeviceRead }) {
  const python = (device.dependencies.python ?? {}) as Record<string, string | null>;
  const tool = (name: string) => {
    const info = device.dependencies[name] as { installed?: boolean; version?: string } | undefined;
    return info?.installed ? `Installed${info.version ? ` (${info.version})` : ''}` : 'Not found';
  };
  return (
    <div className="flex flex-col gap-6">
      <ul className="flex flex-col gap-3">
        {Object.entries(device.capabilities).map(([key, capability]) => (
          <li key={key} className="flex gap-3">
            {capability.supported ? (
              <CircleCheck className="mt-0.5 size-4 shrink-0 text-success" aria-label="Available" />
            ) : (
              <CircleX
                className="mt-0.5 size-4 shrink-0 text-subtle-foreground"
                aria-label="Unavailable"
              />
            )}
            <div>
              <p className="text-[13px] font-medium">{CAPABILITY_LABELS[key] ?? key}</p>
              <p className="text-xs leading-relaxed text-muted-foreground">{capability.reason}</p>
            </div>
          </li>
        ))}
      </ul>
      <dl className="divide-y divide-border border-y border-border">
        <Row label="System">
          {device.os_version} · {device.arch}
        </Row>
        <Row label="CPU">
          {device.cpu_model} ({device.cpu_cores_physical ?? '?'} cores /{' '}
          {device.cpu_cores_logical ?? '?'} threads)
        </Row>
        <Row label="Memory">{formatBytes(device.ram_total_bytes)}</Row>
        <Row label="GPUs">
          {device.gpus.length === 0
            ? 'None detected'
            : device.gpus.map((gpu, i) => {
                const g = gpu as {
                  name: string;
                  vram_mb?: number | null;
                  metal?: string | null;
                  cuda?: boolean;
                };
                return (
                  <span key={i} className="block">
                    {g.name}
                    {g.vram_mb ? ` · ${formatBytes(g.vram_mb * 1024 * 1024)}` : ''}
                    {g.cuda ? ' · CUDA' : ''}
                    {g.metal ? ` · ${g.metal.replace('metal', 'Metal ')}` : ''}
                  </span>
                );
              })}
        </Row>
        <Row label="Free disk">{formatBytes(device.disk_free_bytes)}</Row>
        <Row label="COLMAP">{tool('colmap')}</Row>
        <Row label="Blender">{tool('blender')}</Row>
        <Row label="Python packages">
          {Object.entries(python).map(([name, version]) => (
            <Badge key={name} variant={version ? 'success' : 'neutral'} className="mb-1 mr-1">
              {name} {version ?? '—'}
            </Badge>
          ))}
        </Row>
      </dl>
      <p className="text-xs text-subtle-foreground">
        Detected {formatRelativeTime(device.last_seen_at)}. Installation guidance:{' '}
        <code className="font-mono">docs/LOCAL_GPU_SETUP.md</code>
      </p>
    </div>
  );
}

export function SettingsScreen() {
  const { desktop } = useStudio();
  const info = useSystemInfo();
  const device = useLocalDevice();

  return (
    <PageContainer className="max-w-4xl">
      <PageHeader
        title="Settings"
        description="AI providers, installation details and local processing capabilities."
      />
      <div className="flex flex-col gap-6">
        <Card>
          <CardHeader>
            <CardTitle>Backend</CardTitle>
            <CardDescription>
              Where your projects are stored and the upload limits in effect.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {info.isError ? (
              <ErrorState error={info.error} onRetry={() => void info.refetch()} />
            ) : info.isPending ? (
              <Skeleton className="h-32" />
            ) : (
              <dl className="divide-y divide-border border-y border-border">
                <Row label="Version">{info.data.version}</Row>
                <Row label="Environment">{info.data.environment}</Row>
                <Row label="Database">{info.data.database_backend}</Row>
                <Row label="Storage">{info.data.storage_backend}</Row>
                <Row label="Data folder">
                  {info.data.data_dir ? (
                    <span className="flex flex-wrap items-center gap-2">
                      <code className="font-mono text-xs">{info.data.data_dir}</code>
                      {desktop ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => void desktop.openDataFolder()}
                        >
                          <FolderOpen aria-hidden /> Open
                        </Button>
                      ) : null}
                    </span>
                  ) : (
                    'Hidden by server configuration'
                  )}
                </Row>
                <Row label="Storage used">{formatBytes(info.data.storage_usage_bytes)}</Row>
                <Row label="Upload limit">
                  {formatBytes(info.data.limits.max_upload_bytes, 0)} per photo ·{' '}
                  {info.data.limits.max_images_per_project} photos per project
                </Row>
              </dl>
            )}
          </CardContent>
        </Card>

        <ProviderSettings />

        <Card>
          <CardHeader className="flex-row items-start justify-between gap-4">
            <div className="flex flex-col gap-1">
              <CardTitle>This machine</CardTitle>
              <CardDescription>
                What can run locally. Automatic processing uses this to choose a route.
              </CardDescription>
            </div>
            <Button
              size="sm"
              variant="secondary"
              onClick={() => device.refresh.mutate()}
              disabled={device.refresh.isPending}
            >
              <RotateCw
                aria-hidden
                className={device.refresh.isPending ? 'animate-spin' : undefined}
              />
              Re-detect
            </Button>
          </CardHeader>
          <CardContent>
            {device.isError ? (
              <ErrorState error={device.error} onRetry={() => void device.refetch()} />
            ) : device.isPending ? (
              <Skeleton className="h-48" />
            ) : (
              <DeviceDetails device={device.data} />
            )}
          </CardContent>
        </Card>

        {info.data ? (
          <Card>
            <CardHeader>
              <CardTitle>Quality modes</CardTitle>
              <CardDescription>
                Profiles are translated into each provider’s own settings; not every provider
                supports every option.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <dl className="divide-y divide-border border-y border-border">
                {info.data.quality_modes.map((mode) => (
                  <Row key={mode.id} label={mode.label}>
                    <span className="text-muted-foreground">{mode.description}</span>
                  </Row>
                ))}
              </dl>
            </CardContent>
          </Card>
        ) : null}
      </div>
    </PageContainer>
  );
}
