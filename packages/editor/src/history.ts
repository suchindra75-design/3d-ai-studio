/**
 * Undo/redo for workspace edits.
 *
 * Every edit is a command that can apply and revert itself. The history also
 * knows whether the model differs from the version it was loaded from, and
 * produces the human-readable change list saved with a new version.
 */

export interface Command {
  /** Short description, e.g. `Moved "Handle"`. */
  label: string;
  apply(): void;
  revert(): void;
}

export const DEFAULT_HISTORY_LIMIT = 200;

export class EditHistory {
  private done: Command[] = [];
  private undone: Command[] = [];
  /** Edits pushed out of the undo stack by the limit; they can no longer be undone. */
  private dropped = 0;
  private version = 0;
  private readonly listeners = new Set<() => void>();

  constructor(private readonly limit = DEFAULT_HISTORY_LIMIT) {}

  /** Applies a command and records it. */
  execute(command: Command): void {
    command.apply();
    this.record(command);
  }

  /** Records a command whose effect is already applied (e.g. at the end of a gizmo drag). */
  record(command: Command): void {
    this.done.push(command);
    if (this.done.length > this.limit) {
      this.done.shift();
      this.dropped += 1;
    }
    this.undone = [];
    this.bump();
  }

  undo(): Command | undefined {
    const command = this.done.pop();
    if (!command) return undefined;
    command.revert();
    this.undone.push(command);
    this.bump();
    return command;
  }

  redo(): Command | undefined {
    const command = this.undone.pop();
    if (!command) return undefined;
    command.apply();
    this.done.push(command);
    this.bump();
    return command;
  }

  get canUndo(): boolean {
    return this.done.length > 0;
  }

  get canRedo(): boolean {
    return this.undone.length > 0;
  }

  get undoLabel(): string | null {
    return this.done.at(-1)?.label ?? null;
  }

  get redoLabel(): string | null {
    return this.undone.at(-1)?.label ?? null;
  }

  /** True when the model no longer matches the version it was loaded from. */
  get dirty(): boolean {
    return this.done.length > 0 || this.dropped > 0;
  }

  /** Applied edits, oldest first, with consecutive repeats folded ("Moved "Cup" (×3)"). */
  changes(): string[] {
    const folded: { label: string; count: number }[] = [];
    for (const { label } of this.done) {
      const last = folded.at(-1);
      if (last?.label === label) last.count += 1;
      else folded.push({ label, count: 1 });
    }
    const list = folded.map(({ label, count }) => (count > 1 ? `${label} (×${count})` : label));
    if (this.dropped > 0) list.unshift(`${this.dropped} earlier edits`);
    return list;
  }

  /** Forget everything, e.g. after the edits were saved as a new version. */
  clear(): void {
    this.done = [];
    this.undone = [];
    this.dropped = 0;
    this.bump();
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  getVersion = (): number => this.version;

  private bump(): void {
    this.version += 1;
    for (const listener of this.listeners) listener();
  }
}
