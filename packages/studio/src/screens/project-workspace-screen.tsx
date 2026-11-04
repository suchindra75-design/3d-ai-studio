'use client';

import type { ProjectRead } from '@ai3d/shared-types';
import {
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
  Input,
  Label,
  Skeleton,
  Textarea,
} from '@ai3d/ui';
import { formatBytes, pluralize } from '@ai3d/utilities';
import { useQueryClient } from '@tanstack/react-query';
import { Archive, ArchiveRestore, ChevronLeft, MoreHorizontal, Pencil, Trash2 } from 'lucide-react';
import { useId, useState, type FormEvent } from 'react';

import {
  invalidateProjectData,
  useCaptureAssessment,
  useMeasureOverlap,
  useDeleteImage,
  useDeleteProject,
  useImages,
  useJobs,
  useModels,
  useProject,
  useSystemInfo,
  useUpdateProject,
} from '../api/hooks';
import { CaptureGuide } from '../components/capture-guide';
import { CapturePanel } from '../components/capture-panel';
import { Dropzone } from '../components/dropzone';
import { GenerationPanel } from '../components/generation-panel';
import { ImageDetails } from '../components/image-details';
import { ImageGrid } from '../components/image-grid';
import { JobList } from '../components/job-list';
import { ModelList } from '../components/model-list';
import { ErrorState, PageContainer, PageHeader } from '../components/page';
import { UploadQueue } from '../components/upload-queue';
import { useApi, useStudio } from '../context';
import { inputModeLabel, processingModeLabel, qualityModeLabel } from '../labels';
import { routes } from '../routes';
import { enqueueUploads } from '../uploads/store';

const DEFAULT_MAX_UPLOAD = 50 * 1024 * 1024;

/** Mounted only while the dialog is open, so its state starts fresh each time. */
function EditProjectForm({ project, onDone }: { project: ProjectRead; onDone: () => void }) {
  const update = useUpdateProject(project.id);
  const nameId = useId();
  const descriptionId = useId();
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    update.mutate({ name: name.trim(), description: description.trim() }, { onSuccess: onDone });
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={nameId}>Name</Label>
        <Input
          id={nameId}
          value={name}
          maxLength={200}
          onChange={(event) => setName(event.target.value)}
          aria-invalid={!name.trim() || undefined}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={descriptionId}>Description</Label>
        <Textarea
          id={descriptionId}
          value={description}
          maxLength={5000}
          onChange={(event) => setDescription(event.target.value)}
        />
      </div>
      {update.isError ? <ErrorState error={update.error} /> : null}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" disabled={update.isPending || !name.trim()}>
          Save
        </Button>
      </div>
    </form>
  );
}

function EditProjectDialog({
  project,
  open,
  onOpenChange,
}: {
  project: ProjectRead;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="Edit project">
      <EditProjectForm project={project} onDone={() => onOpenChange(false)} />
    </Dialog>
  );
}

function ProjectActions({ project }: { project: ProjectRead }) {
  const { navigation } = useStudio();
  const update = useUpdateProject(project.id);
  const remove = useDeleteProject();
  const [editing, setEditing] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const archived = project.status === 'archived';

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="secondary" size="icon" aria-label="Project actions">
            <MoreHorizontal />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => setEditing(true)}>
            <Pencil aria-hidden /> Edit details
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => update.mutate({ status: archived ? 'active' : 'archived' })}
          >
            {archived ? <ArchiveRestore aria-hidden /> : <Archive aria-hidden />}
            {archived ? 'Restore from archive' : 'Archive'}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem destructive onSelect={() => setConfirmingDelete(true)}>
            <Trash2 aria-hidden /> Delete project…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <EditProjectDialog project={project} open={editing} onOpenChange={setEditing} />
      <ConfirmDialog
        open={confirmingDelete}
        onOpenChange={setConfirmingDelete}
        title={`Delete “${project.name}”?`}
        description={`This permanently deletes the project and its ${pluralize(project.image_count, 'photo')} from this machine. Archive it instead to keep the files.`}
        confirmLabel="Delete project"
        destructive
        onConfirm={() =>
          remove.mutate(project.id, { onSuccess: () => navigation.replace(routes.projects) })
        }
      />
    </>
  );
}

export function ProjectWorkspaceScreen({ projectId }: { projectId: string }) {
  const api = useApi();
  const { Link } = useStudio().navigation;
  const queryClient = useQueryClient();
  const project = useProject(projectId);
  const images = useImages(projectId);
  const capture = useCaptureAssessment(projectId);
  const measureOverlap = useMeasureOverlap(projectId);
  const systemInfo = useSystemInfo();
  const deleteImage = useDeleteImage();
  const jobs = useJobs(projectId);
  const models = useModels(projectId);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const maxBytes = systemInfo.data?.limits.max_upload_bytes ?? DEFAULT_MAX_UPLOAD;
  const imageItems = images.data?.items ?? [];
  const selected = imageItems.find((image) => image.id === selectedId) ?? null;

  function handleFiles(files: File[]) {
    void enqueueUploads({
      api,
      projectId,
      files,
      maxBytes,
      onSettled: () => void invalidateProjectData(queryClient),
    });
  }

  if (project.isError) {
    return (
      <PageContainer>
        <ErrorState error={project.error} onRetry={() => void project.refetch()} />
      </PageContainer>
    );
  }

  const back = (
    <Link
      href={routes.projects}
      className="inline-flex items-center gap-1 text-xs text-subtle-foreground hover:text-foreground"
    >
      <ChevronLeft className="size-3.5" aria-hidden /> Projects
    </Link>
  );

  if (project.isPending) {
    return (
      <PageContainer>
        {back}
        <Skeleton className="mt-3 h-8 w-64" />
        <Skeleton className="mt-6 h-40 w-full" />
      </PageContainer>
    );
  }

  const p = project.data;
  return (
    <PageContainer className="max-w-7xl">
      <PageHeader
        eyebrow={back}
        title={p.name}
        description={
          <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Badge variant="outline">{inputModeLabel[p.input_mode]}</Badge>
            <span>{qualityModeLabel[p.default_quality_mode]}</span>
            <span aria-hidden>·</span>
            <span>{processingModeLabel[p.default_processing_mode]} processing</span>
            <span aria-hidden>·</span>
            <span>{p.allow_cloud_upload ? 'Cloud upload allowed' : 'Photos stay local'}</span>
            {p.status === 'archived' ? <Badge>Archived</Badge> : null}
          </span>
        }
        actions={<ProjectActions project={p} />}
      />
      {p.description ? (
        <p className="-mt-3 mb-6 max-w-3xl text-sm text-muted-foreground">{p.description}</p>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="flex min-w-0 flex-col gap-4">
          <Dropzone onFiles={handleFiles} maxBytes={maxBytes} compact={imageItems.length > 0} />
          <UploadQueue projectId={projectId} />

          {images.isError ? (
            <ErrorState error={images.error} onRetry={() => void images.refetch()} />
          ) : images.isPending ? (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4">
              {Array.from({ length: 4 }, (_, i) => (
                <Skeleton key={i} className="aspect-square" />
              ))}
            </div>
          ) : imageItems.length > 0 ? (
            <section aria-labelledby="photos-heading" className="flex flex-col gap-3">
              <div className="flex items-baseline justify-between">
                <h2 id="photos-heading" className="text-sm font-medium">
                  Photos
                </h2>
                <span className="text-xs text-subtle-foreground">
                  {pluralize(imageItems.length, 'photo')} · {formatBytes(p.total_image_bytes)}
                </span>
              </div>
              <ImageGrid images={imageItems} selectedId={selectedId} onSelect={setSelectedId} />
            </section>
          ) : null}

          <ModelList models={models.data?.items ?? []} projectId={projectId} />
          <JobList jobs={jobs.data?.items ?? []} projectId={projectId} />
        </div>

        <aside className="flex flex-col gap-4 lg:sticky lg:top-6 lg:self-start">
          {selected ? (
            <ImageDetails
              key={selected.id}
              image={selected}
              deleting={deleteImage.isPending}
              onClose={() => setSelectedId(null)}
              onDelete={() =>
                deleteImage.mutate(selected.id, { onSuccess: () => setSelectedId(null) })
              }
            />
          ) : null}
          {images.data ? <GenerationPanel project={p} images={imageItems} /> : null}
          <CapturePanel
            assessment={capture.data}
            loading={capture.isPending}
            measuring={measureOverlap.isPending}
            measureError={measureOverlap.error}
            onMeasureOverlap={() => measureOverlap.mutate()}
            onSelectImage={setSelectedId}
          />
          <CaptureGuide mode={p.input_mode} />
        </aside>
      </div>
    </PageContainer>
  );
}
