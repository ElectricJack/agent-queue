import { useCallback, useEffect, useRef, useState } from "react";
import type { LayoutNode } from "@aq/ts-client";
import { fetchList, type Variant } from "../../../api/graphLayout";
import { TaskCard } from "../TaskNode";
import type { SelectableTask } from "../types";
import type { TaskFilters } from "../taskFilters";
import { taskNodeData } from "./flowNodes";
import { registerLayoutRefetch } from "./liveRegistry";

const PAGE_SIZE = 50;
const LAYOUT_POLL_MS = 2000;
const MAX_INDENT = 3;

interface Props {
  projectId: string;
  variant: Variant;
  filters: TaskFilters;
  expanded: ReadonlySet<string>;
  toggleExpanded: (id: string) => void;
  onTaskClick: (id: string, task?: SelectableTask) => void;
  onFocus?: (id: string) => void;
  selectedTaskId?: string | null;
}

/**
 * The phone view of a tiled layout: the same server ordering as the canvas,
 * paged instead of positioned. Paging state is plain component state — there
 * is no viewport to reconcile, so the tile store would only add bookkeeping.
 */
export default function MobileLayoutList({
  projectId, variant, filters, expanded, toggleExpanded, onTaskClick, onFocus, selectedTaskId,
}: Props) {
  const [nodes, setNodes] = useState<LayoutNode[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const retry = useRef<ReturnType<typeof setTimeout> | null>(null);
  const busy = useRef(false);
  // A page that lands after the filters changed describes the previous query.
  const generation = useRef(0);
  const key = JSON.stringify({
    projectId, variant, expanded: [...expanded].sort(), q: filters.query.trim(), status: filters.status,
  });

  const loadPage = useCallback(async (after: string | null, reset: boolean) => {
    if (busy.current) return;
    busy.current = true;
    const mine = generation.current;
    try {
      const params = JSON.parse(key) as { q: string; status: string; expanded: string[] };
      const page = await fetchList(projectId, {
        variant, expanded: params.expanded, q: params.q, status: params.status,
        cursor: after, limit: PAGE_SIZE,
      });
      if (generation.current !== mine) return;
      if ("pending" in page) {
        // The layout job is still running: an empty list here would read as an
        // empty project. Say so, and come back for it.
        setBuilding(true);
        setError(null);
        retry.current = setTimeout(() => { void loadPageRef.current(after, reset); }, LAYOUT_POLL_MS);
        return;
      }
      setBuilding(false);
      const fetched = page.nodes ?? [];
      setNodes((previous) => reset ? fetched : [...previous, ...fetched]);
      setCursor(page.next_cursor ?? null);
      setDone(!page.next_cursor);
      setError(null);
    } catch (e) {
      if (generation.current === mine) { setError(e as Error); setBuilding(false); }
    } finally {
      busy.current = false;
    }
  }, [projectId, variant, key]);

  const loadPageRef = useRef(loadPage);
  loadPageRef.current = loadPage;
  useEffect(() => () => { if (retry.current) clearTimeout(retry.current); }, []);

  useEffect(() => {
    generation.current += 1;
    if (retry.current) clearTimeout(retry.current);
    setNodes([]);
    setCursor(null);
    setDone(false);
    setBuilding(false);
    void loadPage(null, true);
  }, [loadPage]);

  // Live updates rebuild the first page: a flat list has no visible cells to
  // reconcile, and later pages are re-fetched as the reader scrolls again.
  useEffect(
    () => registerLayoutRefetch(projectId, () => {
      generation.current += 1;
      void loadPage(null, true);
    }),
    [projectId, loadPage],
  );

  const sentinel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = sentinel.current;
    if (!element || done || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry?.isIntersecting) void loadPage(cursor, false);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [cursor, done, loadPage]);

  const context = {
    projectId, offsetY: 0, expanded,
    handlers: { onOpenTask: onTaskClick, onToggleChildren: toggleExpanded, onFocus: onFocus ?? (() => {}) },
  };

  return (
    <div role="region" aria-label="Task list" className="h-full space-y-3 overflow-y-auto p-3">
      {error && <p role="alert" className="text-sm text-amber-200">Could not load tasks. {error.message}</p>}
      {building && <p role="status" className="py-6 text-center text-sm text-gray-400">Laying out…</p>}
      {nodes.map((node) => (
        <div key={node.id} style={{ marginLeft: Math.min(node.depth, MAX_INDENT) * 12 }}>
          <TaskCard fluid selected={selectedTaskId === node.id} data={taskNodeData(node, context, [])} />
        </div>
      ))}
      {done && nodes.length === 0 && !error && !building &&
        <p className="py-6 text-center text-sm text-gray-500">No tasks match these filters.</p>}
      {!done && !building && <button type="button" onClick={() => void loadPage(cursor, false)}
        className="w-full rounded border border-gray-700 py-2 text-xs text-gray-300 hover:bg-gray-800">Load more</button>}
      <div ref={sentinel} />
    </div>
  );
}

interface ListsProps extends Omit<Props, "projectId"> {
  projectIds: string[];
  projectNames: Map<string, string>;
}

/**
 * The all-projects phone view. Each project pages independently - the daemon
 * orders within a project, and there is no cross-project ordering to preserve.
 */
export function MobileLayoutLists({ projectIds, projectNames, ...rest }: ListsProps) {
  if (projectIds.length === 1) return <MobileLayoutList {...rest} projectId={projectIds[0]!} />;
  return (
    <div className="h-full overflow-y-auto">
      {projectIds.map((pid) => (
        <section key={pid}>
          <h2 className="sticky top-0 z-10 bg-gray-950 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-gray-400">
            {projectNames.get(pid) ?? pid}
          </h2>
          <MobileLayoutList {...rest} projectId={pid} />
        </section>
      ))}
    </div>
  );
}
