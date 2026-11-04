'use client';

import { Skeleton } from '@ai3d/ui';
import { lazy, Suspense, useSyncExternalStore } from 'react';

import { useModel, useProject } from '../api/hooks';
import { ErrorState, PageContainer } from '../components/page';

// three.js and the editor load only when a model is opened.
const WorkspaceView = lazy(() => import('../workspace/workspace-view'));

const subscribeNothing = () => () => {};

function WorkspaceSkeleton() {
  return (
    <div className="flex flex-col gap-3 p-4" aria-busy="true">
      <Skeleton className="h-8 w-72" />
      <Skeleton className="h-[58dvh] w-full lg:h-[calc(100dvh-7rem)]" />
    </div>
  );
}

/** The 3D workspace for one model: inspect, edit and save new versions. */
export function ModelWorkspaceScreen({
  projectId,
  modelId,
}: {
  projectId: string;
  modelId: string;
}) {
  const model = useModel(modelId);
  const project = useProject(projectId);
  // The viewer needs the browser (WebGL, canvas); never render it on the server.
  const mounted = useSyncExternalStore(
    subscribeNothing,
    () => true,
    () => false,
  );

  const error = model.error ?? project.error;
  if (error) {
    return (
      <PageContainer>
        <ErrorState
          error={error}
          onRetry={() => {
            void model.refetch();
            void project.refetch();
          }}
        />
      </PageContainer>
    );
  }
  if (!model.data || !project.data || !mounted) return <WorkspaceSkeleton />;
  if (model.data.project_id !== projectId) {
    return (
      <PageContainer>
        <ErrorState error={new Error('This model belongs to a different project.')} />
      </PageContainer>
    );
  }
  return (
    <Suspense fallback={<WorkspaceSkeleton />}>
      <WorkspaceView model={model.data} projectId={projectId} projectName={project.data.name} />
    </Suspense>
  );
}
