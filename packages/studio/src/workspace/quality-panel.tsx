'use client';

import type { ModelVersionRead } from '@ai3d/shared-types';
import { Badge, Switch, cn } from '@ai3d/ui';
import { formatBytes } from '@ai3d/utilities';
import { formatCount, type ProblemEdges } from '@ai3d/viewer';
import { CircleCheck, CircleMinus, CircleX, Info, TriangleAlert } from 'lucide-react';
import type { ReactNode } from 'react';

import { reportStatusLabel, reportStatusTone } from '../labels';
import { scaleOf, sizeLabel } from './scale';

type Metrics = Record<string, unknown>;

function num(value: unknown): number | null {
  return typeof value === 'number' ? value : null;
}

function yesNo(value: unknown, unknown = 'Not measured'): string {
  return value === true ? 'Yes' : value === false ? 'No' : unknown;
}

function Group({ title, rows }: { title: string; rows: [string, ReactNode][] }) {
  return (
    <section className="flex flex-col gap-2 border-b border-border px-4 py-4">
      <h3 className="text-xs font-medium">{title}</h3>
      <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1.5 text-xs">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-subtle-foreground">{label}</dt>
            <dd className="text-right tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

const severityIcon = {
  error: <CircleX className="size-3.5 shrink-0 text-danger" aria-label="Error" />,
  warning: <TriangleAlert className="size-3.5 shrink-0 text-warning" aria-label="Warning" />,
  info: <Info className="size-3.5 shrink-0 text-subtle-foreground" aria-label="Note" />,
} as const;

const stepIcon = {
  applied: <CircleCheck className="size-3.5 shrink-0 text-success" aria-label="Applied" />,
  skipped: (
    <CircleMinus className="size-3.5 shrink-0 text-subtle-foreground" aria-label="Skipped" />
  ),
  unavailable: <CircleX className="size-3.5 shrink-0 text-warning" aria-label="Unavailable" />,
} as const;

interface StepRecord {
  code: string;
  title: string;
  status: keyof typeof stepIcon;
  detail: string;
}

/** The version's quality report: facts about the stored file, not about the real object. */
export function QualityPanel({
  version,
  showProblems = false,
  onShowProblems,
  problems = null,
}: {
  version: ModelVersionRead;
  /** Draw open and non-manifold edges on the model (see ProblemOverlay). */
  showProblems?: boolean;
  onShowProblems?: (show: boolean) => void;
  problems?: ProblemEdges | null;
}) {
  const report = version.quality_report;
  if (!report) {
    return <p className="px-4 py-4 text-xs text-subtle-foreground">This version has no report.</p>;
  }
  const m = report.metrics as Metrics;
  const scale = scaleOf(version);
  const size = Array.isArray(m.bbox_size) ? (m.bbox_size as number[]) : null;
  const textures = Array.isArray(m.texture_sizes) ? (m.texture_sizes as number[][]) : [];
  const steps = Array.isArray(m.enhancement) ? (m.enhancement as StepRecord[]) : [];
  const recon = (m.reconstruction ?? null) as Metrics | null;
  const uvOutside = num(m.uv_out_of_range);

  return (
    <div className="flex flex-col" aria-label="Quality report">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
        <Badge variant={reportStatusTone[report.status]}>{reportStatusLabel[report.status]}</Badge>
        <span className="text-[11px] text-subtle-foreground">
          Report v{report.report_version} · checked by re-opening the stored file
        </span>
      </div>
      {onShowProblems ? (
        <div className="flex flex-col gap-1.5 border-b border-border px-4 py-3 text-xs">
          <label className="flex items-center justify-between gap-3">
            <span>Show problems on the model</span>
            <Switch
              checked={showProblems}
              onCheckedChange={onShowProblems}
              aria-label="Show problems on the model"
            />
          </label>
          {showProblems && problems ? (
            <p role="status" className="leading-relaxed text-muted-foreground">
              {problems.boundaryCount === 0 && problems.nonManifoldCount === 0
                ? 'No open or non-manifold edges in the model as shown.'
                : [
                    problems.boundaryCount > 0
                      ? `${formatCount(problems.boundaryCount)} open edges (red)`
                      : null,
                    problems.nonManifoldCount > 0
                      ? `${formatCount(problems.nonManifoldCount)} non-manifold edges (orange)`
                      : null,
                  ]
                    .filter(Boolean)
                    .join(' · ')}
              . Drawn through the surface; measured on the model as shown, including unsaved edits.
            </p>
          ) : null}
        </div>
      ) : null}
      <Group
        title="Geometry"
        rows={[
          ['Triangles', formatCount(num(m.triangle_count) ?? 0)],
          ...(num(version.stats?.quads) // quads are kept beside the GLB's triangles (D60)
            ? ([
                [
                  'Quads (from retopology)',
                  `${formatCount(num(version.stats?.quads)!)}: the GLB stores each as two triangles; OBJ and FBX exports keep them as quads`,
                ],
              ] as [string, string][])
            : []),
          ['Vertices', formatCount(num(m.vertex_count) ?? 0)],
          [
            'Separate pieces',
            num(m.components) === null ? 'Not measured' : formatCount(num(m.components)!),
          ],
          ['Watertight (closed)', yesNo(m.watertight)],
          [
            'Open (boundary) edges',
            num(m.boundary_edges) === null ? '—' : formatCount(num(m.boundary_edges)!),
          ],
          [
            'Non-manifold edges',
            num(m.non_manifold_edges) === null ? '—' : formatCount(num(m.non_manifold_edges)!),
          ],
          ['Consistent normals', yesNo(m.winding_consistent, 'Undefined (non-manifold)')],
          [
            'Faces point outwards',
            m.inside_out === true
              ? 'No (inside out)'
              : m.inside_out === false
                ? 'Yes'
                : 'Not measured (not closed)',
          ],
          [
            'Duplicate triangles',
            num(m.duplicate_faces) === null ? '—' : formatCount(num(m.duplicate_faces)!),
          ],
          ['Zero-area triangles', formatCount(num(m.degenerate_triangles) ?? 0)],
        ]}
      />
      <Group
        title="Surface"
        rows={[
          ['Materials', formatCount(num(m.material_count) ?? 0)],
          [
            'Textures',
            textures.length > 0
              ? textures.map(([w, h]) => `${w}×${h}`).join(', ')
              : formatCount(num(m.texture_count) ?? 0),
          ],
          ['UV coordinates', yesNo(m.has_uv)],
          ['UVs outside 0–1', uvOutside === null ? '—' : `${Math.round(uvOutside * 100)}%`],
          ['File size', formatBytes(num(m.file_bytes) ?? 0)],
        ]}
      />
      <Group
        title="Size"
        rows={[
          [
            'Scale',
            scale ? `Known (${scale.axis.toUpperCase()} = ${scale.length_mm} mm)` : 'Unknown',
          ],
          ...(size
            ? (['X', 'Y', 'Z'].map((axis, i) => [
                `${axis} extent`,
                sizeLabel(size[i] ?? 0, Boolean(scale)),
              ]) as [string, ReactNode][])
            : []),
          ...(num(m.volume) !== null && scale
            ? ([['Volume', `${(num(m.volume)! * 1e6).toPrecision(3)} cm³`]] as [
                string,
                ReactNode,
              ][])
            : []),
        ]}
      />
      {recon ? (
        <Group
          title="Reconstruction"
          rows={[
            ['Photos placed', `${recon.registered_images} of ${recon.total_images}`],
            ['Sparse points', formatCount(num(recon.sparse_points) ?? 0)],
            ['Mean reprojection error', `${num(recon.mean_reprojection_error_px)?.toFixed(2)} px`],
            ['Surface type', recon.dense ? 'Dense' : 'From sparse points (coarse)'],
          ]}
        />
      ) : null}
      {steps.length > 0 ? (
        <section className="flex flex-col gap-2 border-b border-border px-4 py-4">
          <h3 className="text-xs font-medium">Enhancement steps</h3>
          <ul className="flex flex-col gap-1.5 text-xs">
            {steps.map((step) => (
              <li key={step.code} className="flex gap-2">
                {stepIcon[step.status]}
                <span>
                  <span className={cn(step.status === 'applied' && 'font-medium')}>
                    {step.title}
                  </span>
                  <span className="text-muted-foreground"> — {step.detail}</span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {report.issues.length > 0 ? (
        <section className="flex flex-col gap-2 px-4 py-4">
          <h3 className="text-xs font-medium">Findings</h3>
          <ul className="flex flex-col gap-1.5 text-xs">
            {report.issues.map((issue) => (
              <li key={issue.code} className="flex gap-2 leading-relaxed">
                {severityIcon[issue.severity as keyof typeof severityIcon] ?? severityIcon.info}
                <span className={issue.severity === 'info' ? 'text-muted-foreground' : ''}>
                  {issue.message}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <p className="px-4 pb-4 text-[11px] leading-relaxed text-subtle-foreground">
        These are facts about the file. They do not say how closely the model matches the real
        object; AI-generated geometry is not a measurement.
      </p>
    </div>
  );
}
