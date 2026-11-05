'use client';

/**
 * The 3D workspace (loaded on demand: it pulls in three.js). One
 * `VersionWorkspace` is mounted per viewed version, so switching versions
 * always starts from the stored file with an empty undo history.
 */
import {
  ClipPreview,
  EditError,
  EditHistory,
  deleteCommand,
  exportGlb,
  TransformGizmo,
  useHistoryState,
  type Command,
  type GizmoMode,
} from '@ai3d/editor';
import type { ModelRead, ModelVersionRead } from '@ai3d/shared-types';
import { Button, ConfirmDialog, Progress, Spinner, cn } from '@ai3d/ui';
import {
  CAMERA_PRESETS,
  CameraLink,
  DEFAULT_VIEWPORT_OPTIONS,
  MeasureMarkers,
  ProblemOverlay,
  Viewport,
  buildHierarchy,
  findProblemEdges,
  formatCount,
  measure,
  sceneStats,
  sourceMaterials,
  webglAvailable,
  type CameraPreset,
  type ViewRequest,
  type ViewportOptions,
} from '@ai3d/viewer';
import { ChevronLeft, Download, Redo2, Save, Undo2 } from 'lucide-react';
import {
  Component,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import * as THREE from 'three';

import { useModelVersions, useSaveModelVersion, useUpdateModel } from '../api/hooks';
import { DownloadButton, versionFileName } from '../components/download-button';
import { ErrorState } from '../components/page';
import { useApi, useStudio } from '../context';
import { versionSourceLabel } from '../labels';
import { routes } from '../routes';
import { CutPanel, defaultCut, draftPlane, type CutDraft } from './edit-tools';
import { EnhancePanel } from './enhance-panel';
import { ExportPanel } from './export-panel';
import { HierarchyPanel } from './hierarchy-panel';
import { InspectorPanel } from './inspector-panel';
import { useModelFile } from './model-file';
import { QualityPanel } from './quality-panel';
import { RetexturePanel } from './retexture-panel';
import { scaleOf } from './scale';
import { VersionPanel } from './version-panel';
import {
  CompareTable,
  CompareViewport,
  MeasurePanel,
  SectionControls,
  defaultSection,
  distanceLabel,
  saveScreenshot,
  sectionPlane,
  type SectionState,
} from './view-tools';
import { GizmoControls, ViewportControls } from './viewport-controls';

class CanvasBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  override state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  override render() {
    if (this.state.error) {
      return (
        <ViewportMessage title="The 3D view stopped working">
          {this.state.error.message}. The model data and panels are still available.
        </ViewportMessage>
      );
    }
    return this.props.children;
  }
}

function ViewportMessage({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div role="alert" className="flex h-full items-center justify-center p-6 text-center">
      <div className="max-w-sm">
        <p className="text-sm font-medium">{title}</p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{children}</p>
      </div>
    </div>
  );
}

function isTyping(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  return (
    element !== null &&
    (element.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName))
  );
}

/** Above this many triangles shadows start off (they draw the model twice). */
const LARGE_MODEL_TRIANGLES = 1_500_000;

const SHORTCUTS: [string, string][] = [
  ['Drag · right-drag · scroll', 'Orbit · pan · zoom'],
  ['Click', 'Select a part'],
  ['W / E / R', 'Move / rotate / scale tool'],
  ['F or 0, 1–6', 'Fit, camera views'],
  ['Esc', 'Clear selection and measurement'],
  ['M', 'Measuring tool on/off'],
  ['Delete / Backspace', 'Delete the selected part'],
  ['⌘/Ctrl Z, ⇧⌘/Ctrl Z', 'Undo, redo'],
];

interface VersionWorkspaceProps {
  model: ModelRead;
  version: ModelVersionRead;
  versions: ModelVersionRead[];
  projectId: string;
  projectName: string;
  notice: string | null;
  onView: (versionId: string, notice?: string) => void;
}

function VersionWorkspace({
  model,
  version,
  versions,
  projectId,
  projectName,
  notice,
  onView,
}: VersionWorkspaceProps) {
  const api = useApi();
  const { Link } = useStudio().navigation;
  const file = useModelFile(api, version.file_url);
  const [history] = useState(() => new EditHistory());
  const edits = useHistoryState(history);
  const [preview, setPreview] = useState(0);
  const [options, setOptions] = useState<ViewportOptions>(DEFAULT_VIEWPORT_OPTIONS);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const [view, setView] = useState<ViewRequest>({ preset: 'iso', nonce: 0 });
  const [gizmo, setGizmo] = useState<GizmoMode>('translate');
  const [tab, setTab] = useState<'inspect' | 'cut' | 'quality' | 'enhance' | 'export' | 'versions'>(
    'inspect',
  );
  const [cutDraft, setCutDraft] = useState<CutDraft | null>(null);
  const [pendingView, setPendingView] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [webgl] = useState(webglAvailable);
  const [measuring, setMeasuring] = useState(false);
  const [points, setPoints] = useState<THREE.Vector3[]>([]);
  const [section, setSection] = useState<SectionState | null>(null);
  const [compareId, setCompareId] = useState<string | null>(null);
  const [link] = useState(() => new CameraLink());
  const [viewNotice, setViewNotice] = useState<string | null>(null);
  const [showProblems, setShowProblems] = useState(false);
  const capture = useRef<(() => Promise<Blob>) | null>(null);
  const onCaptureReady = useCallback((fn: () => Promise<Blob>) => {
    capture.current = fn;
  }, []);
  const { desktop } = useStudio();
  const save = useSaveModelVersion(projectId, model.id);
  const update = useUpdateModel(projectId, model.id);

  const loaded = file.status === 'ready' ? file.model : null;
  const root = loaded?.root ?? null;
  // eslint-disable-next-line react-hooks/exhaustive-deps -- re-read after edits (deleted parts)
  const tree = useMemo(() => (root ? buildHierarchy(root) : null), [root, edits.version]);
  const selected =
    root && selectedId ? (root.getObjectByProperty('uuid', selectedId) ?? null) : null;
  const draft = cutDraft ?? (root ? defaultCut(root) : null);
  const clipPlane = useMemo(() => (draft ? draftPlane(draft) : null), [draft]);
  const stats = useMemo(
    () => (root ? sceneStats(root, sourceMaterials) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- recount after edits
    [root, edits.version],
  );
  const revision = edits.version * 100_000 + preview;
  const scaleKnown = scaleOf(version) !== null;
  const sectionClip = useMemo(() => (section ? sectionPlane(section) : null), [section]);
  const markerSize = useMemo(
    () =>
      root ? Math.max(measure(root).getBoundingSphere(new THREE.Sphere()).radius, 1e-3) * 0.012 : 0,
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-measure after edits
    [root, edits.version],
  );
  const problems = useMemo(
    () => (showProblems && root ? findProblemEdges(root) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-measure after edits
    [showProblems, root, edits.version],
  );
  const compareVersion = versions.find((v) => v.id === compareId && v.id !== version.id) ?? null;

  // Large models: shadows double the drawing work, so they start off.
  const large = (stats?.triangles ?? 0) > LARGE_MODEL_TRIANGLES;
  const [largeHandled, setLargeHandled] = useState(false);
  if (large && !largeHandled) {
    // Adjusting state while rendering (once), React's pattern for derived state.
    setLargeHandled(true);
    setOptions((current) => ({ ...current, shadows: false }));
    setViewNotice('Shadows are off for this large model; turn them on under Shadows.');
  }

  async function takeScreenshot() {
    if (!capture.current) return;
    try {
      const blob = await capture.current();
      const name = versionFileName(model.name, version.version_number, 'png');
      const saved = await saveScreenshot(blob, name, desktop);
      if (saved) setViewNotice(`Screenshot saved: ${saved}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The screenshot could not be saved.');
    }
  }

  function pick(point: THREE.Vector3) {
    setPoints((current) => (current.length >= 2 ? [point] : [...current, point]));
  }
  const saving = exporting || save.isPending;
  const nextVersion =
    Math.max(...versions.map((v) => v.version_number), version.version_number) + 1;

  /** Runs a structural edit; shows why when it cannot be done. */
  function runEdit(make: () => Command): boolean {
    try {
      history.execute(make());
      setError(null);
      return true;
    } catch (cause) {
      if (cause instanceof EditError) {
        setError(cause.message);
        return false;
      }
      throw cause;
    }
  }

  function requestView(preset: CameraPreset) {
    setView((current) => ({ preset, nonce: current.nonce + 1 }));
  }

  function requestVersion(id: string) {
    if (history.dirty) setPendingView(id);
    else onView(id);
  }

  function toggleHidden(id: string) {
    const object = root?.getObjectByProperty('uuid', id);
    if (!object) return;
    object.visible = !object.visible;
    setHidden((current) => {
      const next = new Set(current);
      if (object.visible) next.delete(id);
      else next.add(id);
      return next;
    });
    setPreview((n) => n + 1);
  }

  async function saveEdits() {
    if (!loaded) return;
    setError(null);
    setExporting(true);
    try {
      const glb = await exportGlb(loaded.root, loaded.animations);
      const created = await save.mutateAsync({
        file: new Blob([glb], { type: 'model/gltf-binary' }),
        parentVersionId: version.id,
        changes: history.changes(),
      });
      history.clear();
      // Re-open what the backend stored, so the view shows the saved file.
      onView(
        created.id,
        `Saved as version ${created.version_number}. Version ${version.version_number} is unchanged.`,
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'The edits could not be saved.');
    } finally {
      setExporting(false);
    }
  }

  // Keyboard shortcuts (ignored while typing in a field).
  useEffect(() => {
    // The part the Delete key removes (never the whole model).
    const part = selected && selected !== root ? selected : null;
    function onKeyDown(event: KeyboardEvent) {
      if (isTyping(event.target) || event.altKey) return;
      const mod = event.metaKey || event.ctrlKey;
      const key = event.key.toLowerCase();
      if (mod && key === 'z') {
        event.preventDefault();
        if (event.shiftKey) history.redo();
        else history.undo();
        return;
      }
      if (mod && key === 'y') {
        event.preventDefault();
        history.redo();
        return;
      }
      if (mod) return;
      if (key === 'w') setGizmo('translate');
      else if (key === 'e') setGizmo('rotate');
      else if (key === 'r') setGizmo('scale');
      else if (key === 'f') requestView('iso');
      else if (key === 'escape') {
        setSelectedId(null);
        setPoints([]);
      } else if (key === 'm') setMeasuring((on) => !on);
      else if ((key === 'delete' || key === 'backspace') && part) {
        try {
          history.execute(deleteCommand(part));
          setSelectedId(null);
        } catch (cause) {
          if (!(cause instanceof EditError)) throw cause;
        }
      } else {
        const preset = CAMERA_PRESETS.find((p) => p.key === key);
        if (preset) requestView(preset.value);
      }
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [history, selected, root]);

  // Warn before closing the tab with unsaved edits.
  useEffect(() => {
    if (!edits.dirty) return undefined;
    const onBeforeUnload = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, [edits.dirty]);

  const isCurrent = model.current_version?.id === version.id;

  return (
    <div className="flex flex-col lg:h-dvh">
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border px-3 py-2">
        <Button asChild size="sm" variant="ghost" className="-ml-1 px-2">
          <Link href={routes.project(projectId)} aria-label={`Back to ${projectName}`}>
            <ChevronLeft aria-hidden />
            <span className="max-w-40 truncate">{projectName}</span>
          </Link>
        </Button>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-medium">{model.name}</h1>
          <p className="text-[11px] text-subtle-foreground">
            Version {version.version_number} · {versionSourceLabel[version.source]}
            {isCurrent ? ' · current' : ' · not the current version'}
            <span
              role="status"
              aria-live="polite"
              className={edits.dirty ? 'text-warning' : 'text-success'}
            >
              {notice || edits.dirty ? ` · ${edits.dirty ? 'Unsaved edits' : notice}` : ''}
            </span>
          </p>
        </div>
        <div className="flex items-center gap-1">
          <Button
            size="icon"
            variant="ghost"
            aria-label={edits.undoLabel ? `Undo: ${edits.undoLabel}` : 'Undo'}
            title={edits.undoLabel ? `Undo: ${edits.undoLabel}` : 'Nothing to undo'}
            disabled={!edits.canUndo || saving}
            onClick={() => history.undo()}
          >
            <Undo2 aria-hidden />
          </Button>
          <Button
            size="icon"
            variant="ghost"
            aria-label={edits.redoLabel ? `Redo: ${edits.redoLabel}` : 'Redo'}
            title={edits.redoLabel ? `Redo: ${edits.redoLabel}` : 'Nothing to redo'}
            disabled={!edits.canRedo || saving}
            onClick={() => history.redo()}
          >
            <Redo2 aria-hidden />
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={!edits.dirty || saving || !loaded}
            onClick={() => void saveEdits()}
          >
            {saving ? <Spinner label="Saving" /> : <Save aria-hidden />}
            Save as version {nextVersion}
          </Button>
          {version.file_url ? (
            <DownloadButton
              path={version.file_url}
              fileName={versionFileName(model.name, version.version_number, 'glb')}
              label={`Download version ${version.version_number} as GLB`}
            >
              <Download aria-hidden /> GLB
            </DownloadButton>
          ) : null}
        </div>
        {error ? (
          <p role="alert" className="w-full text-xs text-danger">
            {error}
          </p>
        ) : null}
      </header>

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <div
          className={cn(
            'order-1 flex min-w-0 bg-[#0e0e10] lg:order-2 lg:h-auto lg:flex-1',
            compareVersion ? 'h-[80dvh] flex-col md:flex-row' : 'h-[58dvh]',
          )}
        >
          <div className="relative min-h-0 min-w-0 flex-1">
            {file.status === 'loading' ? (
              <div className="flex h-full flex-col items-center justify-center gap-3 p-6">
                <p className="text-xs text-muted-foreground">Opening the 3D file…</p>
                <div className="w-48">
                  {file.progress === null ? (
                    <Spinner />
                  ) : (
                    <Progress value={file.progress * 100} aria-label="Download progress" />
                  )}
                </div>
              </div>
            ) : file.status === 'error' ? (
              <ViewportMessage title="This version could not be opened">
                {file.message}
              </ViewportMessage>
            ) : !webgl ? (
              <ViewportMessage title="3D view unavailable">
                This browser or device does not provide WebGL 2, which the 3D view needs. The
                model’s facts and versions are listed alongside, and the GLB file can be downloaded.
              </ViewportMessage>
            ) : root ? (
              <CanvasBoundary>
                <Viewport
                  className="size-full"
                  model={root}
                  revision={revision}
                  options={options}
                  selectedId={selectedId}
                  onSelect={setSelectedId}
                  view={view}
                  tool={measuring ? 'measure' : 'select'}
                  onPick={pick}
                  clipPlane={tab === 'cut' ? null : sectionClip}
                  cameraLink={compareVersion ? link : undefined}
                  onCaptureReady={onCaptureReady}
                  label={`3D view of ${model.name}, version ${version.version_number}`}
                >
                  {problems ? <ProblemOverlay edges={problems} /> : null}
                  {points.length > 0 ? (
                    <MeasureMarkers
                      points={points}
                      size={markerSize}
                      label={distanceLabel(points, scaleKnown)}
                    />
                  ) : null}
                  {selected && selected !== root && tab !== 'cut' ? (
                    <TransformGizmo object={selected} mode={gizmo} history={history} />
                  ) : null}
                  {tab === 'cut' && clipPlane ? (
                    <ClipPreview plane={clipPlane} root={root} revision={edits.version} />
                  ) : null}
                </Viewport>
                <ViewportControls
                  options={options}
                  onOptions={(change) => setOptions((current) => ({ ...current, ...change }))}
                  onView={requestView}
                  section={section !== null}
                  onSection={(on) => setSection(on ? defaultSection(root) : null)}
                  measuring={measuring}
                  onMeasuring={(on) => {
                    setMeasuring(on);
                    if (!on) setPoints([]);
                  }}
                  onScreenshot={() => void takeScreenshot()}
                />
                <div className="pointer-events-none absolute left-2 top-24 flex flex-col gap-2 sm:top-12">
                  {section && tab !== 'cut' ? (
                    <SectionControls
                      root={root}
                      section={section}
                      onSection={setSection}
                      scaleKnown={scaleKnown}
                      revision={edits.version}
                    />
                  ) : null}
                  {measuring ? (
                    <MeasurePanel
                      points={points}
                      scaleKnown={scaleKnown}
                      measured={version.surface_provenance === 'measured'}
                      onClear={() => setPoints([])}
                    />
                  ) : null}
                  {viewNotice ? (
                    <p
                      role="status"
                      className="pointer-events-auto max-w-64 rounded-md bg-background/85 px-2 py-1 text-[11px] text-muted-foreground"
                    >
                      {viewNotice}{' '}
                      <button
                        type="button"
                        className="underline"
                        onClick={() => setViewNotice(null)}
                      >
                        Dismiss
                      </button>
                    </p>
                  ) : null}
                </div>
                <div className="pointer-events-none absolute inset-x-2 bottom-2 flex flex-wrap items-end justify-between gap-2">
                  {selected && selected !== root ? (
                    <GizmoControls mode={gizmo} onMode={setGizmo} />
                  ) : (
                    <span />
                  )}
                  {stats ? (
                    <p className="rounded-md bg-background/80 px-2 py-1 text-[11px] tabular-nums text-muted-foreground">
                      {formatCount(stats.triangles)} triangles · {formatCount(stats.vertices)}{' '}
                      vertices
                    </p>
                  ) : null}
                </div>
              </CanvasBoundary>
            ) : null}
          </div>
          {compareVersion && webgl ? (
            <div className="relative min-h-0 min-w-0 flex-1 border-t border-border md:border-t-0 md:border-l">
              <CanvasBoundary>
                <CompareViewport
                  version={compareVersion}
                  options={options}
                  view={view}
                  link={link}
                  onClose={() => setCompareId(null)}
                />
              </CanvasBoundary>
            </div>
          ) : null}
        </div>

        <aside
          aria-label="Model parts"
          className="order-2 border-t border-border p-2 lg:order-1 lg:w-60 lg:shrink-0 lg:overflow-y-auto lg:border-t-0 lg:border-r"
        >
          {tree ? (
            <HierarchyPanel
              tree={tree}
              selectedId={selectedId}
              onSelect={setSelectedId}
              hidden={hidden}
              onToggleHidden={toggleHidden}
            />
          ) : (
            <p className="p-2 text-xs text-subtle-foreground">
              The hierarchy appears once the file is open.
            </p>
          )}
        </aside>

        <aside
          aria-label="Inspector"
          className="order-3 border-t border-border lg:w-80 lg:shrink-0 lg:overflow-y-auto lg:border-t-0 lg:border-l"
        >
          <div
            role="tablist"
            aria-label="Inspector sections"
            className="flex overflow-x-auto border-b border-border px-1"
          >
            {(
              [
                ['inspect', 'Inspect'],
                ['cut', 'Cut'],
                ['quality', 'Quality'],
                ['enhance', 'Enhance'],
                ['export', 'Export'],
                ['versions', `Versions (${versions.length})`],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                role="tab"
                id={`tab-${value}`}
                aria-selected={tab === value}
                aria-controls={`panel-${value}`}
                className={cn(
                  '-mb-px shrink-0 border-b-2 px-2.5 py-2.5 text-xs font-medium',
                  tab === value
                    ? 'border-foreground text-foreground'
                    : 'border-transparent text-muted-foreground hover:text-foreground',
                )}
                onClick={() => setTab(value)}
              >
                {label}
              </button>
            ))}
          </div>
          <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
            {tab === 'cut' ? (
              root && draft ? (
                <CutPanel
                  root={root}
                  draft={draft}
                  onDraft={setCutDraft}
                  onEdit={runEdit}
                  scaleKnown={scaleOf(version) !== null}
                  revision={edits.version}
                />
              ) : null
            ) : tab === 'quality' ? (
              <QualityPanel
                version={version}
                showProblems={showProblems}
                onShowProblems={setShowProblems}
                problems={problems}
              />
            ) : tab === 'enhance' ? (
              <>
                <EnhancePanel
                  projectId={projectId}
                  modelId={model.id}
                  version={version}
                  dirty={edits.dirty}
                  onView={onView}
                />
                <RetexturePanel
                  projectId={projectId}
                  modelId={model.id}
                  version={version}
                  onView={onView}
                />
              </>
            ) : tab === 'export' ? (
              <ExportPanel modelName={model.name} version={version} dirty={edits.dirty} />
            ) : tab === 'versions' ? (
              <>
                {compareVersion ? (
                  <div className="flex flex-col gap-2 border-b border-border px-4 py-4">
                    <h2 className="text-xs font-medium">Comparing</h2>
                    <p className="text-[11px] leading-relaxed text-subtle-foreground">
                      Stored facts of both files; the cameras move together.
                      {edits.dirty ? ' The left view also shows your unsaved edits.' : ''}
                    </p>
                    <CompareTable left={version} right={compareVersion} />
                  </div>
                ) : null}
                <VersionPanel
                  modelName={model.name}
                  versions={versions}
                  currentId={model.current_version?.id ?? null}
                  viewingId={version.id}
                  onView={requestVersion}
                  onMakeCurrent={(id) => update.mutate({ current_version_id: id })}
                  onCompare={setCompareId}
                  comparingId={compareVersion?.id ?? null}
                  busy={update.isPending}
                />
              </>
            ) : loaded ? (
              <>
                <InspectorPanel
                  loaded={loaded}
                  version={version}
                  selected={selected}
                  history={history}
                  display={options.display}
                  revision={edits.version}
                  onPreview={() => setPreview((n) => n + 1)}
                  onEdit={runEdit}
                  onDeselect={() => setSelectedId(null)}
                />
                <details className="border-t border-border px-4 py-3 text-xs">
                  <summary className="cursor-pointer text-muted-foreground">
                    Keyboard shortcuts
                  </summary>
                  <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
                    {SHORTCUTS.map(([keys, action]) => (
                      <div key={keys} className="contents">
                        <dt className="font-mono text-[11px] text-subtle-foreground">{keys}</dt>
                        <dd className="text-muted-foreground">{action}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
                <p className="border-t border-border px-4 py-3 text-xs leading-relaxed text-subtle-foreground">
                  Saving re-exports the scene as a new GLB version (three.js exporter); the server
                  re-opens and checks it. Hidden parts are kept (hiding is only a view aid); deleted
                  parts are not. Mesh compression in the original file is not re-applied, so the new
                  file can be larger. Vertex-level mesh editing is not available.
                </p>
              </>
            ) : null}
          </div>
        </aside>
      </div>

      <ConfirmDialog
        open={pendingView !== null}
        onOpenChange={(open) => {
          if (!open) setPendingView(null);
        }}
        title="Discard unsaved edits?"
        description="Opening another version discards the edits you have not saved. Save them as a new version first to keep them."
        confirmLabel="Discard edits"
        destructive
        onConfirm={() => {
          const id = pendingView;
          setPendingView(null);
          if (id) onView(id);
        }}
      />
    </div>
  );
}

/** Picks the version to show (the current one first) and keeps the version list fresh. */
export default function WorkspaceView({
  model,
  projectId,
  projectName,
}: {
  model: ModelRead;
  projectId: string;
  projectName: string;
}) {
  const versions = useModelVersions(model.id);
  const [viewing, setViewing] = useState<{ id: string; notice: string | null } | null>(
    model.current_version ? { id: model.current_version.id, notice: null } : null,
  );

  if (versions.isError) {
    return (
      <div className="p-6">
        <ErrorState error={versions.error} onRetry={() => void versions.refetch()} />
      </div>
    );
  }
  const items = versions.data?.items ?? [];
  const version = items.find((v) => v.id === viewing?.id);
  if (!viewing) {
    return (
      <div className="p-6 text-sm text-muted-foreground">This model has no stored version.</div>
    );
  }
  if (!version) {
    return (
      <div className="flex h-[60dvh] items-center justify-center">
        <Spinner label="Loading versions" />
      </div>
    );
  }
  return (
    <VersionWorkspace
      key={version.id}
      model={model}
      version={version}
      versions={items}
      projectId={projectId}
      projectName={projectName}
      notice={viewing.notice}
      onView={(id, notice) => setViewing({ id, notice: notice ?? null })}
    />
  );
}
