// Live updates arrive far from the canvas (a websocket hook that knows nothing
// about which layers are mounted), so mounted layers register their refetch
// here and the live hook pushes to the project id it heard about.
type Refetch = () => void;

const registry = new Map<string, Set<Refetch>>();

/** Registers `fn` for `projectId`; returns the disposer to call on unmount. */
export function registerLayoutRefetch(projectId: string, fn: Refetch): () => void {
  let listeners = registry.get(projectId);
  if (!listeners) {
    listeners = new Set();
    registry.set(projectId, listeners);
  }
  listeners.add(fn);
  return () => {
    const current = registry.get(projectId);
    if (!current) return;
    current.delete(fn);
    if (current.size === 0) registry.delete(projectId);
  };
}

/** Asks every mounted layer for `projectId` to re-fetch its visible cells. */
export function refetchLayout(projectId: string): void {
  for (const fn of [...(registry.get(projectId) ?? [])]) fn();
}
