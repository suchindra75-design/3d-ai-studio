'use client';

import { cn } from '@ai3d/ui';
import { FolderOpen, Info, Settings } from 'lucide-react';
import type { ReactNode } from 'react';

import { useStudio } from '../context';
import { routes } from '../routes';
import { BackendStatus } from './backend-status';
import { Logo, LogoMark } from './logo';

const NAV = [
  { href: routes.projects, label: 'Projects', icon: FolderOpen },
  { href: routes.settings, label: 'Settings', icon: Settings },
  { href: routes.about, label: 'About', icon: Info },
] as const;

function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Application frame: sidebar on wide screens, top bar on phones. */
export function AppShell({ children }: { children: ReactNode }) {
  const { navigation } = useStudio();
  const { Link } = navigation;
  const pathname = navigation.usePathname();

  return (
    <div className="flex min-h-dvh flex-col md:flex-row">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded-md focus:bg-surface-raised focus:px-3 focus:py-2 focus:text-sm"
      >
        Skip to content
      </a>

      {/* Sidebar (md and up) */}
      <aside className="sticky top-0 hidden h-dvh w-56 shrink-0 flex-col border-r border-border bg-background px-3 py-4 md:flex">
        <Link
          href={routes.projects}
          className="mb-6 px-2 text-foreground"
          aria-label="AI 3D Studio home"
        >
          <Logo />
        </Link>
        <nav aria-label="Main" className="flex flex-col gap-0.5">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = isActive(pathname, href);
            return (
              <Link
                key={href}
                href={href}
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'flex items-center gap-2.5 rounded-md px-2 py-1.5 text-[13px] font-medium transition-colors',
                  active
                    ? 'bg-surface-hover text-foreground'
                    : 'text-muted-foreground hover:bg-surface hover:text-foreground',
                )}
              >
                <Icon className="size-4" aria-hidden />
                {label}
              </Link>
            );
          })}
        </nav>
        <div className="mt-auto px-2">
          <BackendStatus />
        </div>
      </aside>

      {/* Top bar (phones) */}
      <header className="sticky top-0 z-30 flex h-12 items-center justify-between border-b border-border bg-background/95 px-4 backdrop-blur md:hidden">
        <Link href={routes.projects} className="text-foreground" aria-label="AI 3D Studio home">
          <LogoMark />
        </Link>
        <nav aria-label="Main" className="flex items-center gap-1">
          {NAV.map(({ href, label, icon: Icon }) => {
            const active = isActive(pathname, href);
            return (
              <Link
                key={href}
                href={href}
                aria-label={label}
                aria-current={active ? 'page' : undefined}
                className={cn(
                  'flex size-9 items-center justify-center rounded-md',
                  active ? 'bg-surface-hover text-foreground' : 'text-muted-foreground',
                )}
              >
                <Icon className="size-[18px]" aria-hidden />
              </Link>
            );
          })}
          <span className="ml-1">
            <BackendStatus compact />
          </span>
        </nav>
      </header>

      <main id="main" className="min-w-0 flex-1">
        {children}
      </main>
    </div>
  );
}
