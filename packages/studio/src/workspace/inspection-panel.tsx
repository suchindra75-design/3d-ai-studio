'use client';

import type { FindingRead, ModelVersionRead, RepairRequest } from '@ai3d/shared-types';
import { Badge, Button, ConfirmDialog, Input, Progress, Spinner, Switch, cn } from '@ai3d/ui';
import { formatCount } from '@ai3d/viewer';
import { CircleX, Info, TriangleAlert } from 'lucide-react';
import { useRef, useState } from 'react';

import { useInspection, useJob, useRepair, useStartInspection } from '../api/hooks';
import { ErrorState } from '../components/page';
import { useApi } from '../context';
import { newIdempotencyKey } from '../generation';

const CATEGORIES: [FindingRead['category'], string][] = [
  ['integrity', 'Geometry integrity'],
  ['topology', 'Topology'],
  ['surface', 'Surface quality'],
  ['uv_texture', 'UV and texture integrity'],
  ['fabrication', 'Fabrication suitability'],
];

const METHOD_LABEL: Record<FindingRead['method'], string> = {
  exact: 'Exact',
  approximate: 'Approximate',
  sampled: 'Sampled',
};

/** Repairs offered for a finding; what each does is said again before it runs (D70). */
const REPAIR_LABEL: Record<NonNullable<FindingRead['repair']>, string> = {
  recalculate_normals: 'Recalculate normals',
  fix_winding: 'Fix winding',
  remove_degenerate_faces: 'Remove zero-area triangles',
  remove_duplicate_faces: 'Remove duplicate triangles',
  merge_duplicate_vertices: 'Merge duplicate vertices',
  fill_small_holes: 'Fill small holes',
  separate_components: 'Separate pieces',
  remove_components: 'Remove chosen pieces',
  repair_non_manifold: 'Repair non-manifold edges',
};

const severityIcon = {
  error: <CircleX className="size-3.5 shrink-0 text-danger" aria-label="Error" />,
  warning: <TriangleAlert className="size-3.5 shrink-0 text-warning" aria-label="Warning" />,
  info: <Info className="size-3.5 shrink-0 text-subtle-foreground" aria-label="Note" />,
} as const;

/** Highlight colours by severity (drawn through the surface). */
export const SEVERITY_COLOUR = { error: '#ff453a', warning: '#ff9f0a', info: '#64d2ff' } as const;

interface Piece {
  id: number;
  triangles: number;
  closed: boolean;
}

function hasLocations(finding: FindingRead): boolean {
  const l = finding.locations;
  return l.points.length + l.segments.length + l.triangles.length > 0;
}

function countLabel(finding: FindingRead): string {
  const value = Number.isInteger(finding.count) ? formatCount(finding.count) : finding.count;
  return `${value} ${finding.unit}`;
}

function FindingRow({
  finding,
  shown,
  onShow,
  onLocate,
  onRepair,
}: {
  finding: FindingRead;
  shown: boolean;
  onShow: (show: boolean) => void;
  onLocate: () => void;
  onRepair: (body: RepairRequest['operations']) => void;
}) {
  const [chosen, setChosen] = useState<Set<number>>(new Set());
  const pieces = (finding.details.pieces as Piece[] | undefined) ?? [];
  const where = finding.locations;
  return (
    <li className="flex flex-col gap-1.5 border-t border-border py-2.5 first:border-t-0">
      <div className="flex items-start gap-2">
        {severityIcon[finding.severity]}
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <p className="flex flex-wrap items-center gap-1.5 text-xs font-medium">
            {finding.title}
            <span className="tabular-nums text-muted-foreground">· {countLabel(finding)}</span>
            <Badge variant="neutral">{METHOD_LABEL[finding.method]}</Badge>
          </p>
          <p className="text-[11px] leading-relaxed text-muted-foreground">{finding.explanation}</p>
          {finding.meshes.length ? (
            <p className="text-[11px] text-subtle-foreground">In: {finding.meshes.join(', ')}</p>
          ) : null}
          {typeof finding.details.limits === 'string' ? (
            <p className="text-[11px] text-subtle-foreground">Limits: {finding.details.limits}</p>
          ) : null}
          {hasLocations(finding) && where.shown < where.total ? (
            <p className="text-[11px] text-subtle-foreground">
              Showing {formatCount(where.shown)} of {formatCount(where.total)} on the model.
            </p>
          ) : null}
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5 pl-5">
        {hasLocations(finding) ? (
          <>
            <Button
              size="sm"
              variant={shown ? 'secondary' : 'ghost'}
              onClick={() => onShow(!shown)}
            >
              {shown ? 'Hide on model' : 'Show on model'}
            </Button>
            <Button size="sm" variant="ghost" onClick={onLocate}>
              Locate
            </Button>
          </>
        ) : null}
        {finding.repair && finding.repair !== 'separate_components' ? (
          <Button size="sm" variant="ghost" onClick={() => onRepair([{ code: finding.repair! }])}>
            {REPAIR_LABEL[finding.repair]}…
          </Button>
        ) : null}
        {finding.repair === 'separate_components' ? (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => onRepair([{ code: 'separate_components' }])}
          >
            Separate pieces…
          </Button>
        ) : null}
      </div>
      {pieces.length > 1 ? (
        <details className="pl-5 text-[11px]">
          <summary className="cursor-pointer text-muted-foreground">
            Choose pieces to remove ({formatCount(pieces.length)} listed, largest first)
          </summary>
          <ul className="mt-1.5 flex max-h-40 flex-col gap-1 overflow-auto">
            {pieces.map((piece) => (
              <li key={piece.id}>
                <label className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={chosen.has(piece.id)}
                    onChange={(event) =>
                      setChosen((current) => {
                        const next = new Set(current);
                        if (event.target.checked) next.add(piece.id);
                        else next.delete(piece.id);
                        return next;
                      })
                    }
                  />
                  Piece {piece.id}: {formatCount(piece.triangles)} triangles
                  {piece.closed ? ', closed' : ''}
                </label>
              </li>
            ))}
          </ul>
          <Button
            className="mt-1.5"
            size="sm"
            variant="ghost"
            disabled={chosen.size === 0 || chosen.size === pieces.length}
            onClick={() => onRepair([{ code: 'remove_components', pieces: [...chosen] }])}
          >
            Remove {formatCount(chosen.size)} chosen piece{chosen.size === 1 ? '' : 's'}…
          </Button>
        </details>
      ) : null}
    </li>
  );
}

const REPAIR_NOTE: Record<NonNullable<FindingRead['repair']>, string> = {
  recalculate_normals:
    'Normals only: computed from the triangles, smooth across texture seams (hard edges set by hand become smooth). Positions, UVs and materials stay.',
  fix_winding:
    'Turns triangles round so neighbours agree and closed pieces face out. Nothing moves.',
  remove_degenerate_faces:
    'Removes triangles with no area or invalid corners. The surface does not move; a hole can open where such a triangle was the only link.',
  remove_duplicate_faces: 'Removes triangles that repeat another’s corners. Nothing moves.',
  merge_duplicate_vertices:
    'Merges vertices identical in position, UVs and normals. Nothing moves; seams stay.',
  fill_small_holes:
    'Adds flat triangles in openings of three or four edges. The shape changes there. Not done on textured or coloured meshes.',
  separate_components: 'Each piece becomes its own part. No geometry changes.',
  remove_components:
    'Deletes the chosen pieces. This removes geometry you can see; only the pieces you ticked are removed.',
  repair_non_manifold:
    'Removes duplicate and zero-area triangles where they make edges non-manifold. Other non-manifold edges are reported, not changed.',
};

/**
 * The version's health inspection (D69): findings by category with where they are,
 * how they were measured, and the repairs that address them (D70). Inspecting
 * changes nothing; every repair makes a new version.
 */
export function InspectionPanel({
  projectId,
  modelId,
  version,
  shownCode,
  onShow,
  onLocate,
  onView,
}: {
  projectId: string;
  modelId: string;
  version: ModelVersionRead;
  shownCode: string | null;
  onShow: (finding: FindingRead | null) => void;
  onLocate: (finding: FindingRead) => void;
  onView: (versionId: string, notice?: string, compareWith?: string) => void;
}) {
  const api = useApi();
  const inspection = useInspection(version.id);
  const start = useStartInspection(version.id);
  const repair = useRepair(projectId, modelId);
  const [printability, setPrintability] = useState(false);
  const [minWall, setMinWall] = useState('1');
  const [pending, setPending] = useState<RepairRequest['operations'] | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useJob(projectId, modelId, jobId);
  const key = useRef(newIdempotencyKey());
  const state = inspection.data;
  const report = state?.status === 'ready' ? state.report : null;
  const running = state?.status === 'running' || start.isPending;
  const wall = Number(minWall);

  function run() {
    start.mutate({
      printability,
      min_wall_mm: wall > 0 ? wall : 1,
      overhang_degrees: 45,
      self_intersections: 'auto',
    });
  }

  function confirmRepair() {
    if (!pending) return;
    repair.mutate(
      {
        body: { source_version_id: version.id, operations: pending },
        idempotencyKey: key.current,
      },
      {
        onSuccess: (created) => {
          key.current = newIdempotencyKey();
          setJobId(created.id);
        },
      },
    );
    setPending(null);
  }

  const result = job.data;
  const pendingCode = pending?.[0]?.code;
  return (
    <section
      className="flex flex-col gap-3 border-t border-border px-4 py-4"
      aria-label="Health inspection"
    >
      <div className="flex flex-col gap-1">
        <h3 className="text-xs font-medium">Health inspection</h3>
        <p className="text-[11px] leading-relaxed text-subtle-foreground">
          A deeper check of this version: geometry, topology, surface, UVs and textures, and
          optionally 3D printing. Nothing is changed; each repair makes a new version.
        </p>
      </div>
      <label className="flex items-center justify-between gap-3 text-xs">
        <span>Include 3D-printing checks</span>
        <Switch
          checked={printability}
          onCheckedChange={setPrintability}
          aria-label="Include 3D-printing checks"
        />
      </label>
      {printability ? (
        <label className="flex items-center justify-between gap-3 text-xs">
          <span>Thinnest wall to accept (mm)</span>
          <Input
            className="h-7 w-20 text-xs"
            type="number"
            min={0.1}
            step={0.1}
            value={minWall}
            onChange={(event) => setMinWall(event.target.value)}
            aria-label="Thinnest wall to accept (mm)"
          />
        </label>
      ) : null}
      <Button size="sm" variant="secondary" disabled={running} onClick={run}>
        {running ? 'Inspecting…' : report ? 'Inspect again' : 'Inspect model'}
      </Button>
      {start.isError ? <ErrorState error={start.error} /> : null}
      {inspection.isError ? <ErrorState error={inspection.error} /> : null}
      {running ? (
        <div role="status" className="flex items-center gap-2 text-xs text-muted-foreground">
          <Spinner /> Inspecting (large models take a minute or more)…
        </div>
      ) : null}
      {state?.status === 'failed' ? (
        <p role="alert" className="text-xs text-danger">
          {state.message}
        </p>
      ) : null}

      {report ? (
        <div className="flex flex-col gap-3">
          <p className="text-[11px] text-subtle-foreground">
            {formatCount(report.triangles)} triangles · inspected in {report.seconds} s ·{' '}
            {report.findings.length} finding{report.findings.length === 1 ? '' : 's'}. No overall
            score: each section lists what was found and how it was measured.{' '}
            {state?.json_url ? (
              <a className="underline" href={api.url(state.json_url)} download>
                JSON
              </a>
            ) : null}{' '}
            ·{' '}
            {state?.markdown_url ? (
              <a className="underline" href={api.url(state.markdown_url)} download>
                Report
              </a>
            ) : null}
          </p>
          {CATEGORIES.map(([category, label]) => {
            const found = report.findings.filter((f) => f.category === category);
            const skipped = report.skipped.filter((s) => s.category === category);
            if (category === 'fabrication' && !report.options.printability) return null;
            return (
              <div key={category} className="flex flex-col">
                <h4 className="text-[11px] font-medium uppercase tracking-wide text-subtle-foreground">
                  {label}
                </h4>
                {found.length === 0 ? (
                  <p className="py-1.5 text-[11px] text-muted-foreground">
                    No problems found by the checks that ran.
                  </p>
                ) : (
                  <ul aria-label={label}>
                    {found.map((finding) => (
                      <FindingRow
                        key={finding.code}
                        finding={finding}
                        shown={shownCode === finding.code}
                        onShow={(show) => onShow(show ? finding : null)}
                        onLocate={() => onLocate(finding)}
                        onRepair={setPending}
                      />
                    ))}
                  </ul>
                )}
                {skipped.map((s) => (
                  <p key={s.code} className="text-[11px] text-subtle-foreground">
                    {s.title}: {s.reason}
                  </p>
                ))}
              </div>
            );
          })}
        </div>
      ) : null}

      {repair.isError ? <ErrorState error={repair.error} /> : null}
      {result ? (
        <div role="status" aria-live="polite" className="flex flex-col gap-2 text-xs">
          {result.status === 'queued' || result.status === 'running' ? (
            <>
              <span>Repairing… {Math.round(result.progress * 100)}%</span>
              <Progress value={result.progress * 100} aria-label="Repair progress" />
            </>
          ) : result.status === 'succeeded' && result.result_model_version_id ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-success">Repaired: saved as a new version.</span>
              <Button
                size="sm"
                variant="secondary"
                onClick={() =>
                  onView(
                    result.result_model_version_id!,
                    `Repaired from version ${version.version_number}, which is unchanged.`,
                  )
                }
              >
                View the new version
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() =>
                  onView(
                    result.result_model_version_id!,
                    `Repaired from version ${version.version_number}, which is unchanged.`,
                    version.id,
                  )
                }
              >
                Compare before/after
              </Button>
            </div>
          ) : result.status === 'succeeded' ? (
            <span>Nothing needed changing, so no new version was created.</span>
          ) : result.status === 'failed' ? (
            <span className={cn('text-danger')}>
              {result.error?.message ?? 'The repair failed; this version is unchanged.'}
            </span>
          ) : (
            <span>Cancelled.</span>
          )}
        </div>
      ) : null}

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => (open ? undefined : setPending(null))}
        title={pendingCode ? `${REPAIR_LABEL[pendingCode]}?` : ''}
        description={
          pendingCode
            ? `${REPAIR_NOTE[pendingCode]} The result is saved as a new version; version ${version.version_number} is not changed, and you can go back to it from Versions.`
            : ''
        }
        confirmLabel={pendingCode === 'remove_components' ? 'Remove pieces' : 'Repair'}
        destructive={pendingCode === 'remove_components'}
        onConfirm={confirmRepair}
      />
    </section>
  );
}
