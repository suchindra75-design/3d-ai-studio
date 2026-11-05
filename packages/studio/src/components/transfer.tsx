'use client';

/** Importing existing 3D files, backing up a project and restoring a backup. */
import { Button, Dialog, Label } from '@ai3d/ui';
import { ArchiveRestore, FileUp, PackageOpen } from 'lucide-react';
import { useId, useRef, useState } from 'react';

import { useImportModel, useRestoreProject } from '../api/hooks';
import { useApi, useStudio } from '../context';
import { routes } from '../routes';
import { DownloadButton } from './download-button';
import { ErrorState } from './page';

const ACCEPT = '.glb,.obj,.stl,.ply,.3mf,.fbx,.zip';
const selectClass =
  'h-9 rounded-md border border-border-strong bg-background px-2 text-sm outline-none focus:border-accent';

type Units = 'auto' | 'mm' | 'cm' | 'm' | 'in';
type Up = 'auto' | 'y' | 'z';

export function ImportModelButton({ projectId }: { projectId: string }) {
  const ids = { file: useId(), units: useId(), up: useId() };
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [units, setUnits] = useState<Units>('auto');
  const [up, setUp] = useState<Up>('auto');
  const [realScale, setRealScale] = useState(false);
  const imported = useImportModel(projectId);
  const glb = file ? /\.glb$/i.test(file.name) : false;

  function submit() {
    if (!file) return;
    imported.mutate(
      {
        file,
        units: units === 'auto' ? undefined : units,
        upAxis: up === 'auto' ? undefined : up,
        realScale,
      },
      {
        onSuccess: () => {
          setOpen(false);
          setFile(null);
        },
      },
    );
  }

  return (
    <>
      <Button size="sm" variant="secondary" onClick={() => setOpen(true)}>
        <FileUp aria-hidden /> Import 3D file
      </Button>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (!next) imported.reset();
        }}
        title="Import a 3D file"
        description="GLB, OBJ, STL, PLY, 3MF or FBX (FBX needs Blender), or a ZIP with an OBJ and its material and textures (or a glTF with its files). It is converted to GLB and checked like every model."
      >
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <Label htmlFor={ids.file}>File</Label>
            <input
              id={ids.file}
              type="file"
              accept={ACCEPT}
              className="text-sm"
              onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="flex flex-col gap-1">
              <Label htmlFor={ids.units}>Units</Label>
              <select
                id={ids.units}
                className={selectClass}
                value={units}
                disabled={glb}
                onChange={(event) => setUnits(event.target.value as Units)}
              >
                <option value="auto">Usual for the format</option>
                <option value="mm">Millimetres</option>
                <option value="cm">Centimetres</option>
                <option value="m">Metres</option>
                <option value="in">Inches</option>
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor={ids.up}>Up axis</Label>
              <select
                id={ids.up}
                className={selectClass}
                value={up}
                disabled={glb}
                onChange={(event) => setUp(event.target.value as Up)}
              >
                <option value="auto">Usual for the format</option>
                <option value="y">Y up</option>
                <option value="z">Z up</option>
              </select>
            </div>
          </div>
          <p className="text-xs leading-relaxed text-subtle-foreground">
            {glb
              ? 'GLB files are always metres with Y up.'
              : 'Usual: STL and 3MF are millimetres with Z up (as slicers write them); OBJ and PLY metres with Y up.'}
          </p>
          <label className="flex items-start gap-2 text-sm leading-relaxed">
            <input
              type="checkbox"
              className="mt-1 size-3.5 accent-accent"
              checked={realScale}
              onChange={(event) => setRealScale(event.target.checked)}
            />
            <span>
              The file’s size is the real object’s size
              <span className="block text-xs text-subtle-foreground">
                Otherwise sizes are shown in the model’s own units.
              </span>
            </span>
          </label>
          {imported.isError ? <ErrorState error={imported.error} /> : null}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" disabled={!file || imported.isPending} onClick={submit}>
              {imported.isPending ? 'Importing…' : 'Import'}
            </Button>
          </div>
        </div>
      </Dialog>
    </>
  );
}

export function BackupButton({
  projectId,
  projectName,
}: {
  projectId: string;
  projectName: string;
}) {
  const api = useApi();
  const slug =
    projectName
      .replace(/[^A-Za-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .toLowerCase()
      .slice(0, 80) || 'project';
  return (
    <DownloadButton
      path={api.projects.backupPath(projectId)}
      fileName={`${slug}-backup.zip`}
      label="Back up this project as a ZIP"
    >
      <PackageOpen aria-hidden /> Back up
    </DownloadButton>
  );
}

export function RestoreButton() {
  const { navigation } = useStudio();
  const input = useRef<HTMLInputElement>(null);
  const restore = useRestoreProject();
  return (
    <div className="flex flex-col items-end gap-1">
      <Button
        variant="secondary"
        disabled={restore.isPending}
        onClick={() => input.current?.click()}
      >
        <ArchiveRestore aria-hidden /> {restore.isPending ? 'Restoring…' : 'Restore backup'}
      </Button>
      <input
        ref={input}
        type="file"
        accept=".zip"
        className="hidden"
        aria-label="Backup ZIP to restore"
        onChange={(event) => {
          const file = event.target.files?.[0];
          event.target.value = '';
          if (file) {
            restore.mutate(file, {
              onSuccess: (project) => navigation.push(routes.project(project.id)),
            });
          }
        }}
      />
      {restore.isError ? (
        <div className="max-w-sm">
          <ErrorState error={restore.error} />
        </div>
      ) : null}
    </div>
  );
}
