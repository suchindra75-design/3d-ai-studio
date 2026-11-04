import { SettingsScreen } from '@ai3d/studio';
import type { Metadata } from 'next';

export const metadata: Metadata = { title: 'Settings' };

export default function SettingsPage() {
  return <SettingsScreen />;
}
