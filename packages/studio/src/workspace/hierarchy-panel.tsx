'use client';

import { cn } from '@ai3d/ui';
import { formatCount, type HierarchyNode } from '@ai3d/viewer';
import { Box, ChevronRight, Eye, EyeOff, Folder } from 'lucide-react';
import { useState } from 'react';

interface HierarchyProps {
  tree: HierarchyNode;
  selectedId: string | null;
  onSelect: (id: string) => void;
  hidden: ReadonlySet<string>;
  onToggleHidden: (id: string) => void;
}

function NodeRow({
  node,
  depth,
  ...props
}: HierarchyProps & { node: HierarchyNode; depth: number }) {
  const [open, setOpen] = useState(depth < 2);
  const selected = props.selectedId === node.id;
  const isHidden = props.hidden.has(node.id);
  const Icon = node.kind === 'mesh' ? Box : Folder;
  return (
    <li>
      <div
        className={cn(
          'group flex h-7 items-center gap-1 rounded-md pr-1 text-xs',
          selected ? 'bg-accent-soft text-foreground' : 'text-muted-foreground hover:bg-surface',
          isHidden && 'opacity-50',
        )}
        style={{ paddingLeft: depth * 12 }}
      >
        {node.children.length > 0 ? (
          <button
            type="button"
            className="flex size-5 shrink-0 items-center justify-center rounded text-subtle-foreground hover:text-foreground"
            aria-label={open ? `Collapse ${node.name}` : `Expand ${node.name}`}
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            <ChevronRight className={cn('size-3.5 transition-transform', open && 'rotate-90')} />
          </button>
        ) : (
          <span className="size-5 shrink-0" aria-hidden />
        )}
        <button
          type="button"
          aria-current={selected ? 'true' : undefined}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
          onClick={() => props.onSelect(node.id)}
        >
          <Icon className="size-3.5 shrink-0 text-subtle-foreground" aria-hidden />
          <span className="truncate">{node.name}</span>
          <span className="ml-auto shrink-0 tabular-nums text-[11px] text-subtle-foreground">
            {node.triangles > 0 ? formatCount(node.triangles) : ''}
          </span>
        </button>
        {depth > 0 ? (
          <button
            type="button"
            className={cn(
              'flex size-6 shrink-0 items-center justify-center rounded text-subtle-foreground hover:text-foreground',
              !isHidden && 'opacity-0 group-hover:opacity-100 focus-visible:opacity-100',
            )}
            aria-label={isHidden ? `Show ${node.name}` : `Hide ${node.name}`}
            aria-pressed={isHidden}
            onClick={() => props.onToggleHidden(node.id)}
          >
            {isHidden ? <EyeOff className="size-3.5" /> : <Eye className="size-3.5" />}
          </button>
        ) : null}
      </div>
      {open && node.children.length > 0 ? (
        <ul>
          {node.children.map((child) => (
            <NodeRow key={child.id} node={child} depth={depth + 1} {...props} />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

/** The model's scene graph. Hiding a part is a view aid; it is not saved. */
export function HierarchyPanel(props: HierarchyProps) {
  return (
    <section aria-labelledby="hierarchy-heading" className="flex flex-col gap-2">
      <div className="flex items-baseline justify-between px-1">
        <h2 id="hierarchy-heading" className="text-xs font-medium">
          Hierarchy
        </h2>
        <span className="text-[11px] text-subtle-foreground">triangles</span>
      </div>
      <ul aria-label="Model hierarchy">
        <NodeRow node={props.tree} depth={0} {...props} />
      </ul>
    </section>
  );
}
