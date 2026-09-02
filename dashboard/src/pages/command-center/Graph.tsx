import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { usePlaybooks, useSystemStatus } from "../../api/hooks";
import { useShellPaneStore } from "../../panes/store";
import { projectPlaybooks } from "./playbooks";
import { useProjectGraphs } from "../../api/graph";
import { useLayoutExtents } from "../../api/graphLayout";
import GraphCanvas from "./GraphCanvas";
import MobileCardList from "./MobileCardList";
import LayoutCanvas from "./layout-v2/LayoutCanvas";
import MobileLayoutList from "./layout-v2/MobileLayoutList";
import { useExpandedTaskIds } from "./useGraphHierarchy";
import { useJumpTarget } from "./layout-v2/useJumpToResult";
import { useTaskWorkspace } from "./TaskWorkspace";
import { matchesTask } from "./taskFilters";
import { useTaskSelection } from "./useTaskSelection";

function usePortraitMobile() {
  const query = "(max-width: 768px) and (orientation: portrait)";
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const changed = (event: MediaQueryListEvent) => setMatches(event.matches);
    mq.addEventListener("change", changed);
    return () => mq.removeEventListener("change", changed);
  }, []);
  return matches;
}

/** The playbook rail and detail pane are the same either side of the flag. */
function useGraphChrome() {
  const { projectIds, filters } = useTaskWorkspace();
  const { data: definitions = [], isLoading: loadingPlaybooks, error: playbooksError, refetch: retryPlaybooks } = usePlaybooks();
  const { state: pane, open, close } = useShellPaneStore();
  const { selectedTaskId, selectTask, clearTask } = useTaskSelection();
  const selectedPlaybookId = pane.kind === "open" && pane.view === "playbook-detail"
    ? (pane.args as { playbookId: string }).playbookId
    : null;
  const clearSelection = useCallback(() => {
    clearTask();
    if (selectedPlaybookId) close();
  }, [clearTask, selectedPlaybookId, close]);
  const playbooks = useMemo(
    () => projectPlaybooks(definitions, projectIds, filters.query),
    [definitions, projectIds, filters.query],
  );
  const openPlaybook = useCallback((playbookId: string) => open("playbook-detail", { playbookId }), [open]);
  return {
    playbooks, loadingPlaybooks, playbooksError, retryPlaybooks,
    selectedPlaybookId, openPlaybook, selectedTaskId, selectTask, clearSelection,
  };
}

interface ShellProps {
  clearSelection: () => void;
  loadFailed: boolean;
  hasContent: boolean;
  playbooksError: unknown;
  retryPlaybooks: () => void;
  matchingCount: number | null;
  totalCount: number;
  playbookCount: number;
  loadingPlaybooks: boolean;
  loading: boolean;
  overlay: boolean;
  children: ReactNode;
}

function GraphShell(props: ShellProps) {
  const { clearSelection, loadFailed, hasContent, playbooksError, retryPlaybooks, matchingCount,
    totalCount, playbookCount, loadingPlaybooks, loading, overlay, children } = props;
  return (
    <div className="flex h-full min-h-0 flex-col" onClick={(event) => { if (event.target === event.currentTarget) clearSelection(); }}>
      {loadFailed && <p role="alert" className="shrink-0 border-b border-amber-800/50 bg-amber-950/30 px-4 py-2 text-sm text-amber-200">
        {hasContent ? "Some projects could not be loaded. Showing available tasks." : "Could not load tasks. Check the backend connection and try again."}
      </p>}
      {!!playbooksError && <p role="alert" className="shrink-0 border-b border-amber-800/50 px-4 py-2 text-sm text-amber-200">Could not load playbooks. <button className="underline" onClick={() => retryPlaybooks()}>Retry playbooks</button></p>}
      <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-xs text-gray-500" onClick={clearSelection}>
        {/* The tiled view never holds the whole graph, so it can only report
            the server's node count — there is no client-side match set. */}
        <span>{matchingCount === null
          ? `${totalCount} ${totalCount === 1 ? "task" : "tasks"} total`
          : `${matchingCount} matching ${matchingCount === 1 ? "task" : "tasks"} · ${totalCount} total`}</span>
        <span>{playbookCount} playbooks · recurring definitions stay visible</span>
        {loadingPlaybooks && <span role="status">Loading playbooks…</span>}
        <span className="inline-flex items-center gap-1.5"><span className="w-5 border-t-2 border-indigo-400" /> Dependency</span>
        <span className="inline-flex items-center gap-1.5"><span className="w-5 border-t border-dashed border-gray-400" /> Child task</span>
        {loading && <span role="status">Loading tasks…</span>}
      </div>
      <div className="relative min-h-0 flex-1" aria-busy={loading}>
        {children}
        {overlay && <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-gray-950 text-sm text-gray-400">Loading tasks…</div>}
      </div>
    </div>
  );
}

/** The client-side graph: one snapshot per project, laid out in the browser. */
function LegacyGraph() {
  const { projectId, projectIds, projects, filters, isLoadingProjects, projectsError } = useTaskWorkspace();
  const { data: graph, isLoading, errors } = useProjectGraphs(projectIds);
  const chrome = useGraphChrome();
  const { selectTask } = chrome;
  const selectTaskById = useCallback((taskId: string) => {
    const task = graph.tasks.find((candidate) => candidate.id === taskId);
    if (task) selectTask(task);
  }, [graph.tasks, selectTask]);
  const mobile = usePortraitMobile();
  const matchingTaskIds = useMemo(() => {
    const names = new Map(projects.map((p) => [p.id, p.name || p.id]));
    return new Set(graph.tasks.filter((task) => {
      const pid = graph.taskProject[task.id];
      return matchesTask(task, filters, `${pid ?? ""} ${names.get(pid ?? "") ?? ""}`);
    }).map((task) => task.id));
  }, [graph, filters, projects]);
  const loading = isLoading || (!projectId && isLoadingProjects);
  const View = mobile ? MobileCardList : GraphCanvas;

  return (
    <GraphShell clearSelection={chrome.clearSelection}
      loadFailed={!!projectsError || errors.filter(Boolean).length > 0} hasContent={graph.tasks.length > 0}
      playbooksError={chrome.playbooksError} retryPlaybooks={chrome.retryPlaybooks}
      matchingCount={matchingTaskIds.size} totalCount={graph.tasks.length} playbookCount={chrome.playbooks.length}
      loadingPlaybooks={chrome.loadingPlaybooks} loading={loading}
      overlay={loading && graph.tasks.length === 0 && chrome.playbooks.length === 0}>
      <View graph={graph} matchingTaskIds={matchingTaskIds} filtering={!!(filters.query.trim() || filters.status)}
        selectedTaskId={chrome.selectedTaskId} onTaskClick={selectTaskById} onBackgroundClick={chrome.clearSelection}
        playbooks={chrome.playbooks} selectedPlaybookId={chrome.selectedPlaybookId} onPlaybookClick={chrome.openPlaybook} />
    </GraphShell>
  );
}

/** The server-laid-out graph: tiles on demand, so no full snapshot is fetched. */
function LayoutGraph() {
  const { projectId, projectIds, projects, filters, focusId, setFocus, isLoadingProjects, projectsError } = useTaskWorkspace();
  const chrome = useGraphChrome();
  const { selectTask } = chrome;
  const selectTaskById = useCallback((taskId: string) => selectTask({ id: taskId }), [selectTask]);
  const mobile = usePortraitMobile();
  const variant = filters.showCompleted || focusId ? "all" : "active";
  const extents = useLayoutExtents(projectIds, variant);
  const nodeCount = extents.reduce(
    (total, extent) => total + (extent && !("pending" in extent) ? extent.node_count : 0), 0,
  );
  const loading = extents.some((extent) => !extent || "pending" in extent) || (!projectId && isLoadingProjects);
  const projectNames = useMemo(
    () => new Map(projects.map((project) => [project.id, project.name || project.id])),
    [projects],
  );
  const { expandedTaskIds, toggleExpanded } = useExpandedTaskIds();
  const jumpTarget = useJumpTarget();

  return (
    <GraphShell clearSelection={chrome.clearSelection}
      loadFailed={!!projectsError} hasContent={nodeCount > 0}
      playbooksError={chrome.playbooksError} retryPlaybooks={chrome.retryPlaybooks}
      matchingCount={null} totalCount={nodeCount} playbookCount={chrome.playbooks.length}
      loadingPlaybooks={chrome.loadingPlaybooks} loading={loading} overlay={false}>
      {mobile
        ? <MobileLayoutList projectId={projectId ?? projectIds[0] ?? ""} variant={variant} filters={filters}
            expanded={expandedTaskIds} toggleExpanded={toggleExpanded} onFocus={setFocus}
            onTaskClick={selectTaskById} selectedTaskId={chrome.selectedTaskId} />
        : <LayoutCanvas projectIds={projectIds} projectNames={projectNames} variant={variant} filters={filters}
            focusId={focusId} setFocus={setFocus} jumpTarget={jumpTarget}
            selectedTaskId={chrome.selectedTaskId} onTaskClick={selectTaskById} onBackgroundClick={chrome.clearSelection}
            playbooks={chrome.playbooks} selectedPlaybookId={chrome.selectedPlaybookId}
            onPlaybookClick={chrome.openPlaybook} />}
    </GraphShell>
  );
}

export default function CommandCenterGraph() {
  // The flag decides which data path runs at all: the legacy hook must not
  // fetch a full snapshot behind the tiled canvas.
  const layoutV2 = useSystemStatus().data?.graph_layout_enabled === true;
  return layoutV2 ? <LayoutGraph /> : <LegacyGraph />;
}
