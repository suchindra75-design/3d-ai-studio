import { AppShell } from '@ai3d/studio';
import type { ReactNode } from 'react';

export default function StudioLayout({ children }: { children: ReactNode }) {
  return <AppShell>{children}</AppShell>;
}
