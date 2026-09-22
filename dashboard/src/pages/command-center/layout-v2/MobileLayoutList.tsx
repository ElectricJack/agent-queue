import { useCallback, useEffect, useRef, useState } from "react";
import type { LayoutNode } from "@aq/ts-client";
import { fetchList, useLayoutNode, type Variant } from "../../../api/graphLayout";
import { TaskCard } from "../TaskNode";
import type { SelectableTask } from "../types";
import type { TaskFilters } from "../taskFilters";
import { useNavigate } from "react-router-dom";
import Breadcrumbs from "./Breadcrumbs";
import { taskNodeData } from "./flowNodes";
import { registerLayoutRefetch } from "./liveRegistry";
import GraphScopeNotice, { type EmptyReason } from "./GraphScopeNotice";

const PAGE_SIZE = 50;
const LAYOUT_POLL_MS = 2000;
const MAX_INDENT = 3;

interface Props {
  projectId: string;
  projectName?: string;
  variant: Variant;
  filters: TaskFilters;
  /** The container entered, if any: the list shows its direct children. */
  focusId?: string | null;
  onTaskClick: (id: string, task?: SelectableTask) => void;
  onFocus?: (id: string | null) => void;
  selectedTaskId?: string | null;
  setShowCompleted: (show: boolean) => void;
}

/**
 * The phone view of a tiled layout: the same server ordering as the canvas,
 * paged instead of positioned. Paging state is plain component state — there
 * is no viewport to reconcile, so the tile store would only add bookkeeping.
 *
 * Same navigation model as the canvas: a container is a compact card that is
 * never expanded in place, and its enter control goes INTO it.
 */
export default function MobileLayoutList({
  projectId, projectName, variant, filters, focusId, onTaskClick, onFocus, selectedTaskId, setShowCompleted,
}: Props) {
  const [nodes, setNodes] = useState<LayoutNode[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [appliedVariant, setAppliedVariant] = useState<Variant | null>(null);
  const [emptyReason, setEmptyReason] = useState<EmptyReason | null>(null);
  const retry = useRef<ReturnType<typeof setTimeout> | null>(null);
  const busy = useRef(false);
  const appliedVariantRef = useRef<Variant | null>(null);
  // A page that lands after the filters changed describes the previous query.
  const generation = useRef(0);
  // The breadcrumbs' path; the SCOPE is the server's job -- `root` pages the
  // entered container's own children, so a page can never come back short
  // because rows from another scope used it up.
  const { data: focusData } = useLayoutNode(focusId ? projectId : undefined, focusId ?? null);
  const focusNode = focusData && !("pending" in focusData) ? focusData : undefined;
  const key = JSON.stringify({
    projectId, variant, root: focusId ?? null, q: filters.query.trim(), status: filters.status,
  });

  const loadPage = useCallback(async (after: string | null, reset: boolean) => {
    if (busy.current) return;
    busy.current = true;
    const mine = generation.current;
    try {
      const params = JSON.parse(key) as { q: string; status: string; root: string | null };
      const page = await fetchList(projectId, {
        variant, expanded: [], root: params.root, q: params.q, status: params.status,
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
      const responseVariant: Variant = page.variant_applied === "all" ? "all" : "active";
      // A changed response variant cannot safely share a pagination stream:
      // the cursor belongs to the applied layout, not merely the requested one.
      if (!reset && appliedVariantRef.current && responseVariant !== appliedVariantRef.current) {
        generation.current += 1;
        appliedVariantRef.current = null;
        setAppliedVariant(null);
        setEmptyReason(null);
        setNodes([]);
        setCursor(null);
        setDone(false);
        retry.current = setTimeout(() => { void loadPageRef.current(null, true); }, 0);
        return;
      }
      if (reset) {
        appliedVariantRef.current = responseVariant;
        setAppliedVariant(responseVariant);
        setEmptyReason((page.empty_reason as EmptyReason | undefined) ?? null);
      }
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
    appliedVariantRef.current = null;
    setAppliedVariant(null);
    setEmptyReason(null);
    void loadPage(null, true);
  }, [loadPage]);

  // Live updates rebuild the first page: a flat list has no visible cells to
  // reconcile, and later pages are re-fetched as the reader scrolls again.
  useEffect(
    () => registerLayoutRefetch(projectId, () => {
      generation.current += 1;
      appliedVariantRef.current = null;
      setAppliedVariant(null);
      setEmptyReason(null);
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
    projectId, offsetY: 0, focusId,
    handlers: { onOpenTask: onTaskClick, onFocus: onFocus ?? (() => {}) },
  };
  // The container entered is in its own response (it is the scope's root);
  // the breadcrumbs already name it, so the list is its children.
  const shown = focusId ? nodes.filter((node) => node.id !== focusId) : nodes;

  return (
    <div role="region" aria-label="Task list" className="h-full space-y-3 overflow-y-auto p-3">
      {focusId && <Breadcrumbs
        projectName={projectName ?? "Project"}
        ancestors={focusNode?.ancestors?.map((a) => ({ id: a.id, title: a.title })) ?? []}
        current={focusNode ? { id: focusNode.node.id, title: focusNode.node.title } : { id: focusId, title: focusId }}
        onSelect={onFocus ?? (() => {})} />}
      <GraphScopeNotice requestedVariant={variant} appliedVariant={appliedVariant}
        emptyReason={null} showCompleted={filters.showCompleted} onShowCompleted={setShowCompleted}
        showBanner={!!focusId} showEmpty={false} loading={building} error={error} />
      {error && <p role="alert" className="text-sm text-amber-200">Could not load tasks. {error.message}</p>}
      {building && <p role="status" className="py-6 text-center text-sm text-gray-400">Laying out…</p>}
      {shown.map((node) => (
        <div key={node.id} style={{ marginLeft: Math.min(node.depth, MAX_INDENT) * 12 }}>
          <TaskCard fluid selected={selectedTaskId === node.id} data={taskNodeData(node, context, [])} />
        </div>
      ))}
      {done && shown.length === 0 && <GraphScopeNotice requestedVariant={variant} appliedVariant={appliedVariant}
        emptyReason={emptyReason} showCompleted={filters.showCompleted} onShowCompleted={setShowCompleted}
        showBanner={false} loading={building} error={error}
        emptyClassName="flex flex-col items-center gap-2 py-6 text-center text-sm text-gray-500" />}
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
  const navigate = useNavigate();
  if (projectIds.length === 1) {
    return <MobileLayoutList {...rest} projectId={projectIds[0]!}
      projectName={projectNames.get(projectIds[0]!) ?? projectIds[0]!} />;
  }
  return (
    <div className="h-full overflow-y-auto">
      {projectIds.map((pid) => (
        <section key={pid}>
          <h2 className="sticky top-0 z-10 bg-gray-950 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-gray-400">
            {projectNames.get(pid) ?? pid}
          </h2>
          {/* A container belongs to one project, and the `focus` parameter is
            * not project-qualified: entering here would scope the wrong
            * section. Go to that project's own graph instead, already
            * scoped. */}
          <MobileLayoutList {...rest} projectId={pid} projectName={projectNames.get(pid) ?? pid}
            focusId={null}
            onFocus={(id) => { if (id) navigate(`/projects/${pid}/graph?focus=${encodeURIComponent(id)}`); }} />
        </section>
      ))}
    </div>
  );
}
