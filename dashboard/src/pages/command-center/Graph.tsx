import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { usePlaybooks } from "../../api/hooks";
import { useShellPaneStore } from "../../panes/store";
import { projectPlaybooks } from "./playbooks";
import { useLayoutExtents } from "../../api/graphLayout";
import LayoutCanvas from "./layout-v2/LayoutCanvas";
import { MobileLayoutLists } from "./layout-v2/MobileLayoutList";
import { useExpandedTaskIds } from "./useGraphHierarchy";
import { useJumpTarget } from "./layout-v2/useJumpToResult";
import { useTaskWorkspace } from "./TaskWorkspace";
import { useTaskSelection } from "./useTaskSelection";
import type { SelectableTask } from "./types";

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
  totalCount: number;
  playbookCount: number;
  loadingPlaybooks: boolean;
  loading: boolean;
  children: ReactNode;
}

function GraphShell(props: ShellProps) {
  const { clearSelection, loadFailed, hasContent, playbooksError, retryPlaybooks,
    totalCount, playbookCount, loadingPlaybooks, loading, children } = props;
  return (
    <div className="flex h-full min-h-0 flex-col" onClick={(event) => { if (event.target === event.currentTarget) clearSelection(); }}>
      {loadFailed && <p role="alert" className="shrink-0 border-b border-amber-800/50 bg-amber-950/30 px-4 py-2 text-sm text-amber-200">
        {hasContent ? "Some projects could not be loaded. Showing available tasks." : "Could not load tasks. Check the backend connection and try again."}
      </p>}
      {!!playbooksError && <p role="alert" className="shrink-0 border-b border-amber-800/50 px-4 py-2 text-sm text-amber-200">Could not load playbooks. <button className="underline" onClick={() => retryPlaybooks()}>Retry playbooks</button></p>}
      <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-xs text-gray-500" onClick={clearSelection}>
        {/* The tiled view never holds the whole graph, so it can only report
            the server's node count — there is no client-side match set. */}
        <span>{totalCount} {totalCount === 1 ? "task" : "tasks"} total</span>
        <span>{playbookCount} playbooks · recurring definitions stay visible</span>
        {loadingPlaybooks && <span role="status">Loading playbooks…</span>}
        <span className="inline-flex items-center gap-1.5"><span className="w-5 border-t-2 border-indigo-400" /> Dependency</span>
        <span className="inline-flex items-center gap-1.5"><span className="w-5 border-t border-dashed border-gray-400" /> Child task</span>
        {loading && <span role="status">Loading tasks…</span>}
      </div>
      <div className="relative min-h-0 flex-1" aria-busy={loading}>
        {children}
      </div>
    </div>
  );
}

/** The graph tab: server-laid-out tiles on demand, so no full snapshot is fetched. */
export default function CommandCenterGraph() {
  const { projectId, projectIds, projects, filters, focusId, setFocus, isLoadingProjects, projectsError } = useTaskWorkspace();
  const chrome = useGraphChrome();
  const { selectTask } = chrome;
  // The tiled canvas hands back the clicked card's payload, so a task that
  // belongs to a playbook run still opens the run inspector.
  const selectTaskById = useCallback(
    (taskId: string, task?: SelectableTask) => selectTask(task ?? { id: taskId }),
    [selectTask],
  );
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
      totalCount={nodeCount} playbookCount={chrome.playbooks.length}
      loadingPlaybooks={chrome.loadingPlaybooks} loading={loading}>
      {mobile
        ? <MobileLayoutLists projectIds={projectIds} projectNames={projectNames} variant={variant} filters={filters}
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
