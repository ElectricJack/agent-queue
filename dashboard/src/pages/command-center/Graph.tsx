import { useEffect, useMemo, useState } from "react";
import { useProjectGraphs } from "../../api/graph";
import GraphCanvas from "./GraphCanvas";
import MobileCardList from "./MobileCardList";
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

export default function CommandCenterGraph() {
  const { projectId, projectIds, projects, filters, isLoadingProjects, projectsError } = useTaskWorkspace();
  const { data: graph, isLoading, errors } = useProjectGraphs(projectIds);
  const { selectedTaskId, selectTask, clearTask } = useTaskSelection();
  const mobile = usePortraitMobile();
  const matchingTaskIds = useMemo(() => {
    const names = new Map(projects.map((p) => [p.id, p.name || p.id]));
    return new Set(graph.tasks.filter((task) => {
      const pid = graph.taskProject[task.id];
      return matchesTask(task, filters, `${pid ?? ""} ${names.get(pid ?? "") ?? ""}`);
    }).map((task) => task.id));
  }, [graph, filters, projects]);
  const failures = errors.filter(Boolean);
  const loading = isLoading || (!projectId && isLoadingProjects);
  const View = mobile ? MobileCardList : GraphCanvas;

  return (
    <div className="flex h-full min-h-0 flex-col" onClick={(event) => { if (event.target === event.currentTarget) clearTask(); }}>
      {(projectsError || failures.length > 0) && <p role="alert" className="shrink-0 border-b border-amber-800/50 bg-amber-950/30 px-4 py-2 text-sm text-amber-200">
        {graph.tasks.length ? "Some projects could not be loaded. Showing available tasks." : "Could not load tasks. Check the backend connection and try again."}
      </p>}
      <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 text-xs text-gray-500" onClick={clearTask}>
        <span>{matchingTaskIds.size} matching {matchingTaskIds.size === 1 ? "task" : "tasks"} · {graph.tasks.length} total</span>
        <span className="inline-flex items-center gap-1.5"><span className="w-5 border-t-2 border-indigo-400" /> Dependency</span>
        <span className="inline-flex items-center gap-1.5"><span className="w-5 border-t border-dashed border-gray-400" /> Child task</span>
        {loading && <span role="status">Loading tasks…</span>}
      </div>
      <div className="relative min-h-0 flex-1" aria-busy={loading}>
        <View graph={graph} matchingTaskIds={matchingTaskIds} filtering={!!(filters.query.trim() || filters.status)}
          selectedTaskId={selectedTaskId} onTaskClick={selectTask} onBackgroundClick={clearTask} />
        {loading && graph.tasks.length === 0 && <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-gray-950 text-sm text-gray-400">Loading tasks…</div>}
      </div>
    </div>
  );
}
