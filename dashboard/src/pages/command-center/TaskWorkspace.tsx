import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useProjects } from "../../api/hooks";
import { useGraphLive } from "./useGraphLive";
import { FINISHED_STATUSES, readTaskFilters, writeTaskFilters, type TaskFilters } from "./taskFilters";

type Projects = NonNullable<ReturnType<typeof useProjects>["data"]>;
const EMPTY_PROJECTS: Projects = [];
interface TaskWorkspaceValue {
  projectId: string | undefined;
  projectIds: string[];
  projects: Projects;
  isLoadingProjects: boolean;
  projectsError: Error | null;
  filters: TaskFilters;
  focusId: string | null;
  setFocus: (id: string | null) => void;
  setQuery: (query: string) => void;
  setStatus: (status: string) => void;
  setShowCompleted: (show: boolean) => void;
  clearFilters: () => void;
}
const TaskWorkspaceContext = createContext<TaskWorkspaceValue | null>(null);

/** The route is the only project scope; query parameters travel with every tab. */
export function TaskWorkspaceProvider({ children }: { children: ReactNode }) {
  const { projectId } = useParams<{ projectId: string }>();
  const { data: projects = EMPTY_PROJECTS, isLoading: isLoadingProjects, error: projectsError } = useProjects();
  const [params, setParams] = useSearchParams();
  const rawFilters = useMemo(() => readTaskFilters(params), [params]);
  const focusId = rawFilters.focus || null;
  const filters = useMemo(() => ({ ...rawFilters, showCompleted: rawFilters.showCompleted || !!focusId }), [rawFilters, focusId]);
  const projectIds = useMemo(() => projectId ? [projectId] : projects.map((p) => p.id), [projectId, projects]);
  useGraphLive(projectIds);

  const update = useCallback((patch: Partial<TaskFilters>) => {
    setParams((previous) => writeTaskFilters(previous, { ...readTaskFilters(previous), ...patch }), { replace: true });
  }, [setParams]);
  const setQuery = useCallback((query: string) => update({ query }), [update]);
  const setStatus = useCallback((status: string) => {
    update({ status, ...(FINISHED_STATUSES.has(status) ? { showCompleted: true } : {}) });
  }, [update]);
  const setShowCompleted = useCallback((show: boolean) => {
    setParams((previous) => {
      const current = readTaskFilters(previous);
      if (current.focus) return previous;
      return writeTaskFilters(previous, {
        ...current, showCompleted: show,
        status: !show && FINISHED_STATUSES.has(current.status) ? "" : current.status,
      });
    }, { replace: true });
  }, [setParams]);
  const setFocus = useCallback((id: string | null) => update({ focus: id ?? "" }), [update]);
  const clearFilters = useCallback(() => {
    setParams((previous) => {
      const current = readTaskFilters(previous);
      return writeTaskFilters(previous, { query: "", status: "", showCompleted: false, focus: current.focus });
    }, { replace: true });
  }, [setParams]);
  const value = useMemo(() => ({ projectId, projectIds, projects, isLoadingProjects, projectsError,
    filters, focusId, setFocus, setQuery, setStatus, setShowCompleted, clearFilters }),
  [projectId, projectIds, projects, isLoadingProjects, projectsError, filters, focusId, setFocus, setQuery, setStatus, setShowCompleted, clearFilters]);
  return <TaskWorkspaceContext.Provider value={value}>{children}</TaskWorkspaceContext.Provider>;
}

// A paired provider/hook module follows the existing pane-store convention.
// eslint-disable-next-line react-refresh/only-export-components
export function useTaskWorkspace(): TaskWorkspaceValue {
  const value = useContext(TaskWorkspaceContext);
  if (!value) throw new Error("useTaskWorkspace requires TaskWorkspaceProvider");
  return value;
}
