import { Logo } from '@ai3d/studio';
import { Button } from '@ai3d/ui';
import { Camera, Cpu, Gauge, Images, Layers, ShieldCheck } from 'lucide-react';
import Link from 'next/link';

import { Wireframe } from './wireframe';

const METHODS = [
  {
    icon: Camera,
    title: 'One photo · AI generation',
    body: 'A generative model infers a complete shape and texture from a single image. Fast, and good for previews — but the hidden sides are invented, not measured.',
  },
  {
    icon: Images,
    title: 'Many photos · Photogrammetry',
    body: 'Twenty to a few hundred overlapping photos are matched, camera positions are solved and the surface is reconstructed. Geometry comes from your images.',
  },
];

const FEATURES = [
  {
    icon: ShieldCheck,
    title: 'Local-first',
    body: 'Projects live on your disk. Photos are only sent to a cloud provider when you allow it, per project.',
  },
  {
    icon: Layers,
    title: 'Interchangeable engines',
    body: 'Cloud providers and local reconstruction sit behind one interface with discoverable capabilities.',
  },
  {
    icon: Gauge,
    title: 'Fast, High Quality, Professional',
    body: 'Quality profiles translated into each engine’s real settings, with validation and quality reports.',
  },
  {
    icon: Cpu,
    title: 'Honest about hardware',
    body: 'Detects your CPU, GPU, CUDA/Metal support and installed tools, and tells you what can run locally.',
  },
];

const STATUS = [
  { label: 'Projects, photo upload, quality analysis, capture assistant', done: true },
  { label: 'Hardware and dependency detection', done: true },
  { label: 'AI providers (Tripo, Meshy) and generation jobs', done: false },
  { label: '3D workspace, photogrammetry, mesh processing, export', done: false },
];

export default function LandingPage() {
  return (
    <div className="min-h-dvh">
      <header className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 md:px-8">
        <Logo />
        <nav className="flex items-center gap-1" aria-label="Site">
          <Button asChild variant="ghost" size="sm">
            <Link href="/about">About</Link>
          </Button>
          <Button asChild variant="primary" size="sm">
            <Link href="/projects">Open Studio</Link>
          </Button>
        </nav>
      </header>

      <main>
        <section className="mx-auto grid max-w-6xl items-center gap-10 px-4 pb-16 pt-10 md:grid-cols-[1.05fr_1fr] md:px-8 md:pt-20">
          <div>
            <p className="mb-5 text-xs font-medium uppercase tracking-[0.14em] text-subtle-foreground">
              Open source · Apache-2.0 · Local-first
            </p>
            <h1 className="text-4xl font-semibold leading-[1.05] tracking-tight sm:text-5xl md:text-6xl">
              Photographs in.
              <br />
              <span className="text-muted-foreground">3D models out.</span>
            </h1>
            <p className="mt-6 max-w-lg text-[17px] leading-relaxed text-muted-foreground">
              AI 3D Studio reconstructs real objects from a single photo or a full photo set — on
              your own machine, with the AI providers you choose.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Button asChild variant="primary" size="lg">
                <Link href="/projects">Open Studio</Link>
              </Button>
              <Button asChild variant="secondary" size="lg">
                <a href="#how-it-works">How it works</a>
              </Button>
            </div>
          </div>
          <Wireframe className="mx-auto w-full max-w-[460px]" />
        </section>

        <section id="how-it-works" className="mx-auto max-w-6xl scroll-mt-8 px-4 py-16 md:px-8">
          <h2 className="text-2xl font-semibold tracking-tight">Two ways to reconstruct</h2>
          <p className="mt-2 max-w-2xl text-muted-foreground">
            They produce different kinds of results, and the app always tells you which one you
            have.
          </p>
          <div className="mt-8 grid gap-4 md:grid-cols-2">
            {METHODS.map(({ icon: Icon, title, body }) => (
              <article key={title} className="rounded-lg border border-border bg-surface p-6">
                <Icon className="size-5 text-muted-foreground" aria-hidden />
                <h3 className="mt-4 font-medium">{title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{body}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="mx-auto max-w-6xl px-4 py-16 md:px-8">
          <div className="grid gap-x-8 gap-y-10 sm:grid-cols-2 lg:grid-cols-4">
            {FEATURES.map(({ icon: Icon, title, body }) => (
              <div key={title}>
                <Icon className="size-5 text-muted-foreground" aria-hidden />
                <h3 className="mt-3 text-sm font-medium">{title}</h3>
                <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">{body}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="mx-auto max-w-6xl px-4 py-16 md:px-8">
          <div className="rounded-lg border border-border bg-surface p-6 md:p-8">
            <h2 className="text-lg font-semibold tracking-tight">Project status</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Early development. Only what is listed as available actually works today.
            </p>
            <ul className="mt-5 grid gap-2.5 sm:grid-cols-2">
              {STATUS.map((item) => (
                <li key={item.label} className="flex items-start gap-2.5 text-sm">
                  <span
                    className={
                      item.done
                        ? 'mt-1.5 size-2 shrink-0 rounded-full bg-success'
                        : 'mt-1.5 size-2 shrink-0 rounded-full border border-subtle-foreground'
                    }
                    aria-hidden
                  />
                  <span className={item.done ? 'text-foreground' : 'text-muted-foreground'}>
                    {item.label}
                    <span className="sr-only">
                      {item.done ? ' (available)' : ' (in development)'}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        </section>
      </main>

      <footer className="mx-auto max-w-6xl border-t border-border px-4 py-8 text-xs text-subtle-foreground md:px-8">
        AI 3D Studio · Apache License 2.0 · No accounts, no telemetry.
      </footer>
    </div>
  );
}
