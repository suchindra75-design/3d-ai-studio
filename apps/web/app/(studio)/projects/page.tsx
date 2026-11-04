import { ProjectsScreen } from '@ai3d/studio';
import type { Metadata } from 'next';

export const metadata: Metadata = { title: 'Projects' };

export default function ProjectsPage() {
  return <ProjectsScreen />;
}
