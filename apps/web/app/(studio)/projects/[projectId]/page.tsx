import { ProjectWorkspaceScreen } from '@ai3d/studio';
import type { Metadata } from 'next';

export const metadata: Metadata = { title: 'Project' };

export default async function ProjectPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  return <ProjectWorkspaceScreen projectId={projectId} />;
}
