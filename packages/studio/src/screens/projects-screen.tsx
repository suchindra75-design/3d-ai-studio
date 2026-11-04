'use client';

import type { ProjectRead, ProjectStatus } from '@ai3d/shared-types';
import { Badge, Button, Input, Skeleton, cn } from '@ai3d/ui';
import { formatBytes, formatRelativeTime, pluralize } from '@ai3d/utilities';
import { Box, Images, Plus, Search } from 'lucide-react';
import { useDeferredValue, useState } from 'react';

import { useProjects } from '../api/hooks';
import { EmptyState, ErrorState, PageContainer, PageHeader } from '../components/page';
import { useApi, useStudio } from '../context';
import { inputModeLabel } from '../labels';
import { routes } from '../routes';

function ProjectCard({ project }: { project: ProjectRead }) {
  const api = useApi();
  const { Link } = useStudio().navigation;
  return (
    <Link
      href={routes.project(project.id)}
      className="group flex flex-col overflow-hidden rounded-lg border border-border bg-surface transition-colors hover:border-border-strong hover:bg-surface-raised"
    >
      <div className="relative aspect-[4/3] overflow-hidden bg-surface-raised">
        {project.cover_image_id ? (
          // eslint-disable-next-line @next/next/no-img-element -- shared component, served by the backend
          <img
            src={api.url(`/api/v1/images/${project.cover_image_id}/thumbnail`)}
            alt=""
            loading="lazy"
            className="size-full object-cover transition-transform duration-300 group-hover:scale-[1.02]"
          />
        ) : (
          <div className="flex size-full items-center justify-center text-subtle-foreground">
            <Box className="size-8" strokeWidth={1.25} aria-hidden />
          </div>
        )}
      </div>
      <div className="flex flex-col gap-1.5 p-3.5">
        <div className="flex items-center justify-between gap-2">
          <h2 className="truncate text-sm font-medium">{project.name}</h2>
          {project.status === 'archived' ? <Badge>Archived</Badge> : null}
        </div>
        <div className="flex items-center gap-2 text-xs text-subtle-foreground">
          <span>{inputModeLabel[project.input_mode]}</span>
          <span aria-hidden>·</span>
          <span>{pluralize(project.image_count, 'photo')}</span>
          {project.total_image_bytes > 0 ? (
            <>
              <span aria-hidden>·</span>
              <span>{formatBytes(project.total_image_bytes)}</span>
            </>
          ) : null}
        </div>
        <p className="text-xs text-subtle-foreground">
          Updated {formatRelativeTime(project.updated_at)}
        </p>
      </div>
    </Link>
  );
}

const TABS: { value: ProjectStatus; label: string }[] = [
  { value: 'active', label: 'Active' },
  { value: 'archived', label: 'Archived' },
];

export function ProjectsScreen() {
  const { Link } = useStudio().navigation;
  const [status, setStatus] = useState<ProjectStatus>('active');
  const [search, setSearch] = useState('');
  const query = useDeferredValue(search.trim());
  const projects = useProjects({ status, q: query || undefined, limit: 100 });

  const newProjectButton = (
    <Button asChild variant="primary">
      <Link href={routes.newProject}>
        <Plus aria-hidden /> New project
      </Link>
    </Button>
  );

  return (
    <PageContainer>
      <PageHeader
        title="Projects"
        description="Each project holds the photos of one object and the 3D models made from them."
        actions={newProjectButton}
      />

      <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div role="tablist" aria-label="Project status" className="flex gap-1">
          {TABS.map((tab) => (
            <button
              key={tab.value}
              role="tab"
              type="button"
              aria-selected={status === tab.value}
              onClick={() => setStatus(tab.value)}
              className={cn(
                'rounded-md px-3 py-1.5 text-[13px] font-medium transition-colors',
                status === tab.value
                  ? 'bg-surface-hover text-foreground'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <label className="relative sm:w-64">
          <span className="sr-only">Search projects</span>
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-subtle-foreground"
            aria-hidden
          />
          <Input
            type="search"
            placeholder="Search projects"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            className="pl-8"
          />
        </label>
      </div>

      {projects.isPending ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="aspect-[4/3.9] rounded-lg" />
          ))}
        </div>
      ) : projects.isError ? (
        <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />
      ) : projects.data.items.length === 0 ? (
        query ? (
          <EmptyState
            icon={<Search />}
            title="No matching projects"
            description={`Nothing in ${status} projects matches “${query}”.`}
          />
        ) : status === 'archived' ? (
          <EmptyState
            icon={<Box />}
            title="No archived projects"
            description="Archive a project to hide it here without deleting anything."
          />
        ) : (
          <EmptyState
            icon={<Images />}
            title="Create your first project"
            description="Start with one clear photo for a quick AI model, or a set of overlapping photos for measured geometry."
            action={newProjectButton}
          />
        )
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {projects.data.items.map((project) => (
            <ProjectCard key={project.id} project={project} />
          ))}
        </div>
      )}
    </PageContainer>
  );
}
