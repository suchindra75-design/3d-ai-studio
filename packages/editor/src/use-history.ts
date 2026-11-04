import { useSyncExternalStore } from 'react';

import type { EditHistory } from './history';

export interface HistoryState {
  /** Increments on every change; use it as the viewer's `revision`. */
  version: number;
  canUndo: boolean;
  canRedo: boolean;
  undoLabel: string | null;
  redoLabel: string | null;
  dirty: boolean;
}

/** Re-renders the component whenever the history changes. */
export function useHistoryState(history: EditHistory): HistoryState {
  const version = useSyncExternalStore(history.subscribe, history.getVersion, history.getVersion);
  return {
    version,
    canUndo: history.canUndo,
    canRedo: history.canRedo,
    undoLabel: history.undoLabel,
    redoLabel: history.redoLabel,
    dirty: history.dirty,
  };
}
