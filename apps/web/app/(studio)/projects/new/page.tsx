import { NewProjectScreen } from '@ai3d/studio';
import type { Metadata } from 'next';

export const metadata: Metadata = { title: 'New project' };

export default function NewProjectPage() {
  return <NewProjectScreen />;
}
