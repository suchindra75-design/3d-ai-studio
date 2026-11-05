'use client';

import {
  captureTransform,
  colorCommand,
  colorHex,
  describeTransform,
  hasColor,
  isPbr,
  materialName,
  scalarCommand,
  transformCommand,
  type EditHistory,
  type ScalarProperty,
} from '@ai3d/editor';
import type { ModelVersionRead } from '@ai3d/shared-types';
import { Badge } from '@ai3d/ui';
import {
  displayName,
  formatCount,
  measure,
  sceneStats,
  sourceMaterials,
  type DisplayMode,
  type LoadedModel,
} from '@ai3d/viewer';
import { useMemo, useRef, type ReactNode } from 'react';
import * as THREE from 'three';

import { provenanceLabel, reportStatusLabel, reportStatusTone } from '../labels';
import { AdjustControls } from './adjust-controls';
import { ModelActions, PartActions, TextureControls, type RunEdit } from './edit-tools';
import { ColorField, LiveSlider, Vec3Field, type Vec3 } from './fields';
import { scaleOf, sizeLabel } from './scale';

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3 border-b border-border px-4 py-4 last:border-b-0">
      <h2 className="text-xs font-medium">{title}</h2>
      {children}
    </section>
  );
}

function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
      {items.map(([label, value]) => (
        <div key={label} className="flex flex-col">
          <dt className="text-subtle-foreground">{label}</dt>
          <dd className="tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function sizeFacts(box: THREE.Box3, scaleKnown: boolean): [string, ReactNode][] {
  if (box.isEmpty()) return [['Size', '—']];
  const size = box.getSize(new THREE.Vector3());
  return [
    ['Width (X)', sizeLabel(size.x, scaleKnown)],
    ['Height (Y)', sizeLabel(size.y, scaleKnown)],
    ['Depth (Z)', sizeLabel(size.z, scaleKnown)],
  ];
}

function TransformEditor({ object, history }: { object: THREE.Object3D; history: EditHistory }) {
  const name = displayName(object);
  const rotation = object.rotation;
  const toDegrees = (r: number) => THREE.MathUtils.radToDeg(r);

  function commit(change: (o: THREE.Object3D) => void) {
    const before = captureTransform(object);
    const probe = object.clone(false);
    change(probe);
    const after = captureTransform(probe);
    history.execute(
      transformCommand(object, before, after, describeTransform(name, before, after)),
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <Vec3Field
        label="Position"
        step={0.01}
        value={object.position.toArray() as Vec3}
        onCommit={(v) => commit((o) => o.position.fromArray(v))}
      />
      <Vec3Field
        label="Rotation (°)"
        step={1}
        value={[toDegrees(rotation.x), toDegrees(rotation.y), toDegrees(rotation.z)]}
        onCommit={(v) =>
          commit((o) =>
            o.rotation.set(
              THREE.MathUtils.degToRad(v[0]),
              THREE.MathUtils.degToRad(v[1]),
              THREE.MathUtils.degToRad(v[2]),
            ),
          )
        }
      />
      <Vec3Field
        label="Scale"
        step={0.01}
        value={object.scale.toArray() as Vec3}
        onCommit={(v) => commit((o) => o.scale.fromArray(v))}
      />
    </div>
  );
}

const SCALARS: { property: ScalarProperty; label: string }[] = [
  { property: 'roughness', label: 'Roughness' },
  { property: 'metalness', label: 'Metalness' },
  { property: 'opacity', label: 'Opacity' },
];

function MaterialEditor({
  root,
  material,
  users,
  history,
  onPreview,
  onEdit,
  revision,
}: {
  root: THREE.Object3D;
  material: THREE.Material;
  users: number;
  history: EditHistory;
  onPreview: () => void;
  onEdit: RunEdit;
  revision: number;
}) {
  // Value before a live preview began, so the recorded edit undoes to it.
  const colorStart = useRef<THREE.Color | null>(null);
  const scalarStart = useRef<{ value: number; transparent: boolean } | null>(null);
  const textured = Object.entries(material).some(
    ([key, value]) => key === 'map' && value instanceof THREE.Texture,
  );

  return (
    <li className="flex flex-col gap-3 rounded-md border border-border bg-background p-3">
      <div className="flex items-baseline justify-between gap-2">
        <p className="truncate text-xs font-medium">{materialName(material)}</p>
        <p className="shrink-0 text-[11px] text-subtle-foreground">
          {users > 1 ? `Shared by ${users} parts` : material.type.replace('Mesh', '')}
        </p>
      </div>
      {hasColor(material) ? (
        <ColorField
          label={textured ? 'Colour (tints the texture)' : 'Colour'}
          value={colorHex(material)}
          onStart={() => {
            colorStart.current = material.color.clone();
          }}
          onPreview={(hex) => {
            material.color.setStyle(hex, THREE.SRGBColorSpace);
            onPreview();
          }}
          onCommit={(hex) => {
            history.execute(colorCommand(material, hex, colorStart.current ?? undefined));
            colorStart.current = null;
          }}
        />
      ) : null}
      {isPbr(material)
        ? SCALARS.map(({ property, label }) => (
            <LiveSlider
              key={property}
              label={label}
              value={material[property]}
              onStart={() => {
                scalarStart.current = {
                  value: material[property],
                  transparent: material.transparent,
                };
              }}
              onPreview={(value) => {
                material[property] = value;
                if (property === 'opacity' && value < 1 && !material.transparent) {
                  material.transparent = true;
                  material.needsUpdate = true;
                }
                onPreview();
              }}
              onCommit={(value) => {
                history.execute(
                  scalarCommand(material, property, value, scalarStart.current ?? undefined),
                );
                scalarStart.current = null;
              }}
            />
          ))
        : null}
      {!hasColor(material) && !isPbr(material) ? (
        <p className="text-xs text-subtle-foreground">
          This material type has no editable settings.
        </p>
      ) : null}
      <TextureControls root={root} material={material} onEdit={onEdit} revision={revision} />
      <AdjustControls
        root={root}
        material={material}
        onEdit={onEdit}
        onPreview={onPreview}
        revision={revision}
      />
    </li>
  );
}

export interface InspectorProps {
  loaded: LoadedModel;
  version: ModelVersionRead;
  selected: THREE.Object3D | null;
  history: EditHistory;
  display: DisplayMode;
  /** Changes whenever the model changed; the panel re-reads it. */
  revision: number;
  onPreview: () => void;
  /** Runs a structural edit (see edit-tools.tsx). */
  onEdit: RunEdit;
  /** Clears the selection, e.g. after the selected part was deleted. */
  onDeselect: () => void;
}

export function InspectorPanel({
  loaded,
  version,
  selected,
  history,
  display,
  revision,
  onPreview,
  onEdit,
  onDeselect,
}: InspectorProps) {
  const root = loaded.root;
  /* eslint-disable react-hooks/exhaustive-deps -- `revision` signals in-place edits */
  const stats = useMemo(() => sceneStats(root, sourceMaterials), [root, revision]);
  const box = useMemo(() => measure(root), [root, revision]);
  const selectedBox = useMemo(() => (selected ? measure(selected) : null), [selected, revision]);
  const selectedStats = useMemo(
    () => (selected ? sceneStats(selected, sourceMaterials) : null),
    [selected, revision],
  );
  /* eslint-enable react-hooks/exhaustive-deps */
  const materials = useMemo(() => {
    if (!selected) return [];
    const users = new Map<THREE.Material, number>();
    root.traverse((object) => {
      if (!(object as THREE.Mesh).isMesh) return;
      for (const material of sourceMaterials(object as THREE.Mesh)) {
        users.set(material, (users.get(material) ?? 0) + 1);
      }
    });
    const own = new Set<THREE.Material>();
    selected.traverse((object) => {
      if ((object as THREE.Mesh).isMesh) {
        for (const material of sourceMaterials(object as THREE.Mesh)) own.add(material);
      }
    });
    return [...own].map((material) => ({ material, users: users.get(material) ?? 1 }));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `revision`: parts may be deleted
  }, [root, selected, revision]);

  const report = version.quality_report;
  const measured = version.surface_provenance === 'measured';
  const scale = scaleOf(version);
  const isPart = selected !== null && selected !== root;

  return (
    <div className="flex flex-col">
      <Section title="Model">
        <div className="flex flex-wrap gap-1.5">
          <Badge variant={measured ? 'success' : 'warning'}>
            {provenanceLabel[version.surface_provenance]}
          </Badge>
          {report ? (
            <Badge variant={reportStatusTone[report.status]}>
              {reportStatusLabel[report.status]}
            </Badge>
          ) : null}
        </div>
        <Facts
          items={[
            ['Parts', formatCount(stats.meshes)],
            ['Triangles', formatCount(stats.triangles)],
            ['Vertices', formatCount(stats.vertices)],
            ['Materials / textures', `${stats.materials} / ${stats.textures}`],
            ...sizeFacts(box, scale !== null),
          ]}
        />
        <p className="text-xs leading-relaxed text-subtle-foreground">
          {scale
            ? `Real-world size from your measurement (${scale.axis.toUpperCase()} = ${scale.length_mm} mm); only as accurate as that measurement.`
            : measured
              ? 'Sizes are in the model’s own units. The shape was measured from the photos, but the scale is unknown (no reference object), so only proportions are meaningful. Enhance can set it from one measurement.'
              : 'Sizes are in the model’s own units. This model has no real-world scale: its shape was not measured from the object.'}
        </p>
        <ModelActions root={root} onEdit={onEdit} />
        {loaded.animations.length > 0 || loaded.extensions.length > 0 ? (
          <p className="text-xs text-subtle-foreground">
            {loaded.animations.length > 0 ? `${loaded.animations.length} animation(s). ` : ''}
            {loaded.extensions.length > 0 ? `glTF extensions: ${loaded.extensions.join(', ')}` : ''}
          </p>
        ) : null}
      </Section>

      <Section title="Selection">
        {!selected ? (
          <p className="text-xs text-subtle-foreground">
            Click a part in the view or the hierarchy to inspect and edit it.
          </p>
        ) : (
          <>
            <div className="flex items-baseline justify-between gap-2">
              <p className="truncate text-[13px] font-medium" data-testid="selection-name">
                {displayName(selected)}
              </p>
              {selectedStats ? (
                <p className="shrink-0 text-xs tabular-nums text-subtle-foreground">
                  {formatCount(selectedStats.triangles)} triangles
                </p>
              ) : null}
            </div>
            {selectedBox ? <Facts items={sizeFacts(selectedBox, scale !== null)} /> : null}
            {isPart ? (
              <>
                <TransformEditor
                  key={`${selected.uuid}:${revision}`}
                  object={selected}
                  history={history}
                />
                <PartActions root={root} part={selected} onEdit={onEdit} onDeleted={onDeselect} />
              </>
            ) : (
              <p className="text-xs text-subtle-foreground">
                This is the whole model (its actions are under Model). Select a part to move,
                rotate, mirror or delete it.
              </p>
            )}
          </>
        )}
      </Section>

      {selected && materials.length > 0 ? (
        <Section title="Materials">
          {display !== 'material' ? (
            <p className="text-xs text-warning">
              Switch the view to Material to see material changes.
            </p>
          ) : null}
          <ul className="flex flex-col gap-2">
            {materials.map(({ material, users }) => (
              <MaterialEditor
                key={`${material.uuid}:${revision}`}
                root={root}
                material={material}
                users={users}
                history={history}
                onPreview={onPreview}
                onEdit={onEdit}
                revision={revision}
              />
            ))}
          </ul>
        </Section>
      ) : null}
    </div>
  );
}
