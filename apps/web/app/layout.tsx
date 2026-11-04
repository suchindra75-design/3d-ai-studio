import '@ai3d/ui/fonts';
import './globals.css';

import type { Metadata, Viewport } from 'next';
import type { ReactNode } from 'react';

import { Providers } from './providers';

export const metadata: Metadata = {
  title: { default: 'AI 3D Studio', template: '%s · AI 3D Studio' },
  description:
    'Open-source, local-first platform that turns photographs of real objects into 3D models.',
  applicationName: 'AI 3D Studio',
};

export const viewport: Viewport = {
  themeColor: '#0b0b0c',
  colorScheme: 'dark',
  width: 'device-width',
  initialScale: 1,
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
