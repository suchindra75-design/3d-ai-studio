import { ModelWorkspaceScreen } from '@ai3d/studio';
import type { Metadata } from 'next';

export const metadata: Metadata = { title: '3D workspace' };

export default async function ModelPage({
  params,
}: {
  params: Promise<{ projectId: string; modelId: string }>;
}) {
  const { projectId, modelId } = await params;
  return <ModelWorkspaceScreen projectId={projectId} modelId={modelId} />;
}
