'use client';

import type { SceneRead } from '@ai3d/shared-types';
import { Badge, Button, ConfirmDialog, Progress } from '@ai3d/ui';
import { formatBytes, formatRelativeTime } from '@ai3d/utilities';
import { Clapperboard, Download, RefreshCw, Trash2 } from 'lucide-react';
import { useRef, useState } from 'react';

import { useDeleteScene, useRenderSceneAgain } from '../api/hooks';
import { useApi, useStudio } from '../context';
import { newIdempotencyKey } from '../generation';
import {
  jobStageLabel,
  qualityModeLabel,
  sceneLightingLabel,
  sceneShotLabel,
  sceneStyleLabel,
} from '../labels';
import { routes } from '../routes';
import { DownloadButton } from './download-button';
import { ErrorState } from './page';
import { ChoiceFields, defaultChoices, type Choices } from './scene-panel';

const FILE_LABELS = {
  video: ['MP4', 'the video'],
  still: ['Still', 'the still image'],
  blend: ['Blender', 'the Blender file'],
  glb: ['GLB', 'the animated GLB'],
} as const;

function RenderAgain({ scene, onDone }: { scene: SceneRead; onDone: () => void }) {
  const render = useRenderSceneAgain(scene.project_id);
  const [choices, setChoices] = useState<Choices>(() => ({
    ...defaultChoices(scene.quality_mode),
    seconds: scene.seconds,
    shot: scene.shot,
    angle: scene.angle,
    lighting: scene.lighting,
    ground: scene.ground,
    style: scene.style,
  }));
  const key = useRef(newIdempotencyKey());
  return (
    <div className="flex flex-col gap-3 rounded-md bg-surface-raised p-3">
      <p className="text-xs leading-relaxed text-muted-foreground">
        The same objects, filmed again with these settings on this computer. Nothing is sent to your
        GPU server.
      </p>
      <ChoiceFields choices={choices} onChange={setChoices} />
      {render.isError ? <ErrorState error={render.error} /> : null}
      <div className="flex justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button
          size="sm"
          variant="primary"
          disabled={render.isPending}
          onClick={() =>
            render.mutate(
              { sceneId: scene.id, body: choices, idempotencyKey: key.current },
              { onSuccess: onDone },
            )
          }
        >
          <RefreshCw aria-hidden /> Render
        </Button>
      </div>
    </div>
  );
}

function SceneCard({ scene }: { scene: SceneRead }) {
  const api = useApi();
  const { Link } = useStudio().navigation;
  const remove = useDeleteScene(scene.project_id);
  const [again, setAgain] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const files = Object.fromEntries(scene.files.map((file) => [file.name, file]));
  const job = scene.job;
  const progress = job ? Math.round(job.progress * 100) : 0;

  return (
    <li className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-3.5">
      <div className="overflow-hidden rounded-md bg-surface-hover">
        {scene.status === 'ready' && files.video ? (
          <video
            controls
            playsInline
            loop
            preload="metadata"
            poster={files.still ? api.url(files.still.url) : undefined}
            src={api.url(files.video.url)}
            className="aspect-video w-full bg-black"
            aria-label={`${scene.title}: rendered shot`}
          />
        ) : (
          <div className="flex aspect-video w-full flex-col items-center justify-center gap-2 p-6 text-center">
            <Clapperboard className="size-6 text-subtle-foreground" aria-hidden />
            {scene.status === 'making' ? (
              <div className="flex w-full max-w-xs flex-col gap-1.5">
                <Progress value={progress} aria-label="Scene progress" />
                <span className="text-xs text-muted-foreground">
                  {job?.status === 'queued'
                    ? 'Waiting for a free worker'
                    : (jobStageLabel[job?.stage ?? ''] ?? 'Working')}{' '}
                  · {progress}%
                </span>
              </div>
            ) : (
              <p role="alert" className="max-w-sm text-xs leading-relaxed text-danger">
                {job?.error?.message ??
                  (scene.status === 'cancelled' ? 'Cancelled.' : 'This scene was not rendered.')}
              </p>
            )}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="truncate text-[13px] font-medium">{scene.title}</span>
          <span className="line-clamp-2 text-xs text-muted-foreground">{scene.prompt}</span>
        </div>
        <span className="text-xs text-subtle-foreground">
          {formatRelativeTime(scene.created_at)}
        </span>
      </div>

      <div className="flex flex-wrap gap-1.5">
        <Badge variant="outline">{sceneShotLabel[scene.shot]}</Badge>
        <Badge variant="outline">{sceneLightingLabel[scene.lighting]}</Badge>
        <Badge variant="outline">{sceneStyleLabel[scene.style]}</Badge>
        <Badge variant="outline">
          {scene.seconds} s{scene.render ? ` · ${scene.render.width}×${scene.render.height}` : ''}
        </Badge>
        <Badge variant="outline">{qualityModeLabel[scene.quality_mode]}</Badge>
      </div>

      {scene.objects.length > 0 ? (
        <p className="flex flex-wrap items-center gap-1.5 text-xs">
          <span className="text-subtle-foreground">Objects:</span>
          {scene.objects.map((object) =>
            object.model_id ? (
              <Link
                key={object.name}
                href={routes.model(scene.project_id, object.model_id)}
                className="text-accent hover:underline"
              >
                {object.name}
              </Link>
            ) : (
              <span key={object.name} className="text-muted-foreground">
                {object.name}
              </span>
            ),
          )}
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        {scene.files.map((file) => (
          <DownloadButton
            key={file.name}
            path={`${file.url}?download=true`}
            fileName={file.filename}
            label={`Download ${FILE_LABELS[file.name][1]} (${formatBytes(file.size_bytes)})`}
          >
            <Download aria-hidden /> {FILE_LABELS[file.name][0]}
          </DownloadButton>
        ))}
        {scene.status === 'ready' && !again ? (
          <Button size="sm" variant="secondary" onClick={() => setAgain(true)}>
            <RefreshCw aria-hidden /> Render again
          </Button>
        ) : null}
        {scene.status !== 'making' ? (
          <Button
            size="sm"
            variant="ghost"
            aria-label={`Delete the scene ${scene.title}`}
            onClick={() => setConfirming(true)}
          >
            <Trash2 aria-hidden />
          </Button>
        ) : null}
      </div>
      {again ? <RenderAgain scene={scene} onDone={() => setAgain(false)} /> : null}
      {remove.isError ? <ErrorState error={remove.error} /> : null}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Delete “${scene.title}”?`}
        description="This deletes the rendered video, still and scene files. Its objects stay in the project as models."
        confirmLabel="Delete scene"
        destructive
        onConfirm={() => remove.mutate(scene.id)}
      />
    </li>
  );
}

export function SceneList({ scenes }: { scenes: SceneRead[] }) {
  if (scenes.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-border p-6 text-center text-[13px] text-muted-foreground">
        No scenes yet. Describe a shot to make the first one.
      </p>
    );
  }
  return (
    <section aria-labelledby="scenes-heading" className="flex flex-col gap-3">
      <h2 id="scenes-heading" className="text-sm font-medium">
        Scenes
      </h2>
      <ul className="grid gap-3 xl:grid-cols-2">
        {scenes.map((scene) => (
          <SceneCard key={scene.id} scene={scene} />
        ))}
      </ul>
    </section>
  );
}
