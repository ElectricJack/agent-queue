export const GRAPH_POSITIONS_STORAGE_KEY = "aq.command-center.graph-positions";
export const GRAPH_POSITIONS_CHANGED = "aq:command-center-graph-positions-changed";
export const PLAYBOOK_POSITION_SCOPE = "__playbooks__";

export interface ManualPosition {
  x: number;
  y: number;
}

export type ManualPositions = Record<string, Record<string, ManualPosition>>;

export function storedGraphPositions(): ManualPositions {
  if (typeof window === "undefined") return {};
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(GRAPH_POSITIONS_STORAGE_KEY) ?? "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const positions: ManualPositions = Object.create(null) as ManualPositions;
    for (const [scope, entries] of Object.entries(parsed)) {
      if (!entries || typeof entries !== "object" || Array.isArray(entries)) continue;
      for (const [id, value] of Object.entries(entries)) {
        if (!value || typeof value !== "object" || Array.isArray(value)) continue;
        const { x, y } = value as Partial<ManualPosition>;
        if (typeof x !== "number" || !Number.isFinite(x) || typeof y !== "number" || !Number.isFinite(y)) continue;
        (positions[scope] ??= {})[id] = { x, y };
      }
    }
    return positions;
  } catch {
    return {};
  }
}

export function saveGraphPosition(scope: string, id: string, position: ManualPosition): ManualPositions {
  const positions = storedGraphPositions();
  positions[scope] = { ...(positions[scope] ?? {}), [id]: position };
  window.localStorage.setItem(GRAPH_POSITIONS_STORAGE_KEY, JSON.stringify(positions));
  return positions;
}

export function clearGraphPositions(scope: string): void {
  const positions = storedGraphPositions();
  if (!(scope in positions)) return;
  delete positions[scope];
  window.localStorage.setItem(GRAPH_POSITIONS_STORAGE_KEY, JSON.stringify(positions));
  window.dispatchEvent(new Event(GRAPH_POSITIONS_CHANGED));
}
