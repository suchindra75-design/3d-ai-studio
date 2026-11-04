'use client';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@ai3d/ui';

import { useHealth } from '../api/hooks';
import { PageContainer, PageHeader } from '../components/page';
import { useStudio } from '../context';

const CREDITS: { name: string; license: string; use: string }[] = [
  { name: 'FastAPI · Starlette · Pydantic', license: 'MIT', use: 'Backend API' },
  { name: 'SQLAlchemy · Alembic', license: 'MIT', use: 'Database and migrations' },
  { name: 'Pillow', license: 'MIT-CMU', use: 'Image decoding, EXIF, thumbnails' },
  { name: 'OpenCV', license: 'Apache-2.0', use: 'Image quality analysis' },
  { name: 'NumPy', license: 'BSD-3-Clause', use: 'Numerics' },
  { name: 'psutil', license: 'BSD-3-Clause', use: 'Hardware detection' },
  { name: 'React · Next.js', license: 'MIT', use: 'Web interface' },
  { name: 'Tauri', license: 'Apache-2.0 / MIT', use: 'Desktop application' },
  { name: 'Radix UI · lucide', license: 'MIT · ISC', use: 'Accessible UI primitives, icons' },
  { name: 'Tailwind CSS', license: 'MIT', use: 'Styling' },
  { name: 'TanStack Query · Zustand', license: 'MIT', use: 'Client state' },
  { name: 'Inter', license: 'OFL-1.1', use: 'Typeface' },
];

const LIMITATIONS = [
  'A single photo cannot show hidden sides: AI generation invents them.',
  'AI-generated meshes are not dimensionally accurate and are not CAD models.',
  'Photogrammetry recovers shape up to an unknown scale unless a reference is provided.',
  'Shiny, transparent and texture-less surfaces reconstruct poorly.',
  'Cloud providers have their own pricing, limits and output licences.',
];

export function AboutScreen() {
  const { platform, runtime = [] } = useStudio();
  const health = useHealth();
  const facts = [
    { label: 'Interface', value: platform === 'desktop' ? 'Desktop app' : 'Web app' },
    ...runtime,
    { label: 'Backend', value: health.data ? `v${health.data.version}` : 'Not connected' },
  ];
  return (
    <PageContainer className="max-w-3xl">
      <PageHeader
        title="About AI 3D Studio"
        description="An open-source, local-first platform for turning photographs into 3D models."
      />
      <div className="flex flex-col gap-6">
        <Card>
          <CardHeader>
            <CardTitle>Open source</CardTitle>
            <CardDescription>
              Licensed under the Apache License 2.0. No accounts, subscriptions or telemetry.
              Third-party model weights are never bundled; each provider or model keeps its own
              licence.
            </CardDescription>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>This installation</CardTitle>
          </CardHeader>
          <CardContent>
            <dl className="divide-y divide-border border-y border-border">
              {facts.map((fact) => (
                <div key={fact.label} className="flex justify-between gap-4 py-2 text-[13px]">
                  <dt className="text-subtle-foreground">{fact.label}</dt>
                  <dd className="text-right">{fact.value}</dd>
                </div>
              ))}
            </dl>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Known limitations</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="flex list-disc flex-col gap-1.5 pl-5 text-[13px] text-muted-foreground">
              {LIMITATIONS.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Built with</CardTitle>
          </CardHeader>
          <CardContent>
            <table className="w-full text-left text-[13px]">
              <thead className="text-xs text-subtle-foreground">
                <tr>
                  <th scope="col" className="pb-2 font-medium">
                    Project
                  </th>
                  <th scope="col" className="pb-2 font-medium">
                    Licence
                  </th>
                  <th scope="col" className="hidden pb-2 font-medium sm:table-cell">
                    Used for
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border border-t border-border">
                {CREDITS.map((credit) => (
                  <tr key={credit.name}>
                    <td className="py-2 pr-3">{credit.name}</td>
                    <td className="py-2 pr-3 text-muted-foreground">{credit.license}</td>
                    <td className="hidden py-2 text-muted-foreground sm:table-cell">
                      {credit.use}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      </div>
    </PageContainer>
  );
}
