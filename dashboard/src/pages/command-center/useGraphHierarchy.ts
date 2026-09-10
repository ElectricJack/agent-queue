import { createContext, createElement, useCallback, useContext, useMemo, useRef, useSyncExternalStore, type ReactNode } from "react";
import { useQueries, useQueryClient } from "@tanstack/react-query";
import {
  conflictDocument, dashboardDocumentKey, defaultDashboardDocument, fetchDashboardDocument,
  putDashboardDocument, useDashboardDocument, type CommandCenterPreferencesValue,
  type CommandCenterProjectViewValue, type DashboardDocument, type DashboardNamespace,
  type DashboardValue, type ManualPosition, type PlaybookGraphViewValue,
} from "../../api/dashboardState";
import { DEFAULT_DENSITY, type LayoutDensity } from "./layout-v2/density";
import { PLAYBOOK_POSITION_SCOPE, type ManualPositions } from "./layout-v2/manualPositions";

interface GraphState {
  expandedTaskIds: ReadonlySet<string>;
  expandedFinishedIds: ReadonlySet<string>;
  toggleExpanded: (id: string, finished?: boolean, projectId?: string) => void;
  setExpandedTaskIds: (ids: ReadonlySet<string>) => void;
  density: LayoutDensity;
  setDensity: (density: LayoutDensity) => void;
  manualPositions: ManualPositions;
  saveGraphPosition: (scope: string, id: string, position: ManualPosition) => void;
  clearGraphPositions: (scope: string) => void;
}

const GraphStateContext = createContext<GraphState | null>(null);

// Isolated component tests do not mount the route provider. This in-memory
// store has intentionally no browser backing; production uses server defaults
// until GraphStateProvider loads the user's documents.
let fallbackExpanded: ReadonlySet<string> = new Set();
let fallbackFinished: ReadonlySet<string> = new Set();
let fallbackSnapshotValue = { expandedTaskIds: fallbackExpanded, expandedFinishedIds: fallbackFinished };
let fallbackDensity: LayoutDensity = DEFAULT_DENSITY;
let fallbackPositions: ManualPositions = {};
const fallbackListeners = new Set<() => void>();
const notifyFallback = () => {
  fallbackSnapshotValue = { expandedTaskIds: fallbackExpanded, expandedFinishedIds: fallbackFinished };
  for (const listener of fallbackListeners) listener();
};

function fallbackSnapshot() { return fallbackSnapshotValue; }
function setFallbackExpanded(next: ReadonlySet<string>) {
  fallbackExpanded = new Set(next);
  fallbackFinished = new Set([...fallbackFinished].filter((id) => fallbackExpanded.has(id)));
  notifyFallback();
}

/** Test helper retained for consumers rendered without their route provider. */
export function setExpandedTaskIds(next: ReadonlySet<string>) {
  fallbackDensity = DEFAULT_DENSITY;
  fallbackPositions = {};
  setFallbackExpanded(next);
}

function useFallbackState(): GraphState {
  const snapshot = useSyncExternalStore(
    (listener) => { fallbackListeners.add(listener); return () => fallbackListeners.delete(listener); }, fallbackSnapshot,
  );
  const toggleExpanded = useCallback((id: string, finished = false) => {
    const next = new Set(fallbackExpanded);
    const opening = !next.delete(id);
    if (opening) next.add(id);
    const nextFinished = new Set(fallbackFinished);
    if (opening && finished) nextFinished.add(id); else nextFinished.delete(id);
    fallbackFinished = nextFinished;
    setFallbackExpanded(next);
  }, []);
  return useMemo(() => ({
    ...snapshot, toggleExpanded, setExpandedTaskIds: setFallbackExpanded, density: fallbackDensity,
    setDensity: (density) => { fallbackDensity = density; notifyFallback(); }, manualPositions: fallbackPositions,
    saveGraphPosition: (scope, id, position) => { fallbackPositions = { ...fallbackPositions, [scope]: { ...fallbackPositions[scope], [id]: position } }; notifyFallback(); },
    clearGraphPositions: (scope) => { const { [scope]: _removed, ...rest } = fallbackPositions; fallbackPositions = rest; notifyFallback(); },
  }), [snapshot, toggleExpanded]);
}

function projectValue(document: DashboardDocument<CommandCenterProjectViewValue>): Required<CommandCenterProjectViewValue> {
  return {
    expanded_task_ids: document.value.expanded_task_ids ?? [],
    expanded_finished_task_ids: document.value.expanded_finished_task_ids ?? [],
    manual_positions: document.value.manual_positions ?? {},
  };
}

/** Shared server state for graph density, hierarchy, and manual positions. */
export function GraphStateProvider({ projectIds, children }: { projectIds: string[]; children: ReactNode }) {
  const queryClient = useQueryClient();
  const preferences = useDashboardDocument<CommandCenterPreferencesValue>("command_center_preferences");
  const playbooks = useDashboardDocument<PlaybookGraphViewValue>("playbook_graph_view");
  const projectQueries = useQueries({ queries: projectIds.map((projectId) => ({
    queryKey: dashboardDocumentKey("command_center_project_view", projectId),
    queryFn: () => fetchDashboardDocument<CommandCenterProjectViewValue>("command_center_project_view", projectId),
  })) });
  const writes = useRef(new Map<string, Promise<void>>());

  const queueUpdate = useCallback(<T extends DashboardValue>(namespace: DashboardNamespace, subject: string | null, change: (value: T) => T) => {
    const key = dashboardDocumentKey(namespace, subject);
    const address = key.join("\u0001");
    const previous = writes.current.get(address) ?? Promise.resolve();
    const next = previous.catch(() => {}).then(async () => {
      const current = (queryClient.getQueryData(key) as DashboardDocument<T> | undefined) ?? defaultDashboardDocument<T>(namespace, subject);
      const value = change(current.value);
      queryClient.setQueryData(key, { ...current, value });
      try {
        queryClient.setQueryData(key, await putDashboardDocument(current, value));
      } catch (error) {
        // Never overwrite another machine's CAS write with a stale browser edit.
        queryClient.setQueryData(key, conflictDocument(error) as DashboardDocument<T> | null ?? current);
      }
    });
    writes.current.set(address, next);
    void next.finally(() => { if (writes.current.get(address) === next) writes.current.delete(address); });
  }, [queryClient]);

  const documents = useMemo(() => new Map(projectIds.map((projectId, index) => [projectId,
    (projectQueries[index]?.data as DashboardDocument<CommandCenterProjectViewValue> | undefined)
      ?? defaultDashboardDocument<CommandCenterProjectViewValue>("command_center_project_view", projectId),
  ])), [projectIds, projectQueries]);
  const expandedTaskIds = useMemo(() => new Set([...documents.values()].flatMap((document) => projectValue(document).expanded_task_ids)), [documents]);
  const expandedFinishedIds = useMemo(() => new Set([...documents.values()].flatMap((document) => projectValue(document).expanded_finished_task_ids)), [documents]);
  const manualPositions = useMemo<ManualPositions>(() => {
    const result: ManualPositions = {};
    for (const [projectId, document] of documents) result[projectId] = projectValue(document).manual_positions;
    result[PLAYBOOK_POSITION_SCOPE] = playbooks.data?.value.manual_positions ?? {};
    return result;
  }, [documents, playbooks.data]);
  const updateProject = useCallback((projectId: string, change: (value: Required<CommandCenterProjectViewValue>) => CommandCenterProjectViewValue) => {
    if (projectId) queueUpdate<CommandCenterProjectViewValue>("command_center_project_view", projectId, (value) => change(projectValue({ ...defaultDashboardDocument<CommandCenterProjectViewValue>("command_center_project_view", projectId), value })));
  }, [queueUpdate]);

  const toggleExpanded = useCallback((id: string, finished = false, projectId?: string) => {
    const subject = projectId ?? [...documents.entries()].find(([, document]) => projectValue(document).expanded_task_ids.includes(id))?.[0] ?? projectIds[0];
    if (!subject) return;
    updateProject(subject, (value) => {
      const expanded = new Set(value.expanded_task_ids);
      const opening = !expanded.delete(id);
      if (opening) expanded.add(id);
      const completed = new Set(value.expanded_finished_task_ids);
      if (opening && finished) completed.add(id); else completed.delete(id);
      return { ...value, expanded_task_ids: [...expanded], expanded_finished_task_ids: [...completed] };
    });
  }, [documents, projectIds, updateProject]);
  const replaceExpanded = useCallback((ids: ReadonlySet<string>) => {
    for (const projectId of projectIds) updateProject(projectId, (value) => ({
      ...value,
      expanded_task_ids: value.expanded_task_ids.filter((id) => ids.has(id)),
      expanded_finished_task_ids: value.expanded_finished_task_ids.filter((id) => ids.has(id)),
    }));
  }, [projectIds, updateProject]);
  const saveGraphPosition = useCallback((scope: string, id: string, position: ManualPosition) => {
    if (scope === PLAYBOOK_POSITION_SCOPE) queueUpdate<PlaybookGraphViewValue>("playbook_graph_view", null, (value) => ({ ...value, manual_positions: { ...(value.manual_positions ?? {}), [id]: position } }));
    else updateProject(scope, (value) => ({ ...value, manual_positions: { ...value.manual_positions, [id]: position } }));
  }, [queueUpdate, updateProject]);
  const clearGraphPositions = useCallback((scope: string) => {
    if (scope === PLAYBOOK_POSITION_SCOPE) queueUpdate<PlaybookGraphViewValue>("playbook_graph_view", null, (value) => ({ ...value, manual_positions: {} }));
    else updateProject(scope, (value) => ({ ...value, manual_positions: {} }));
  }, [queueUpdate, updateProject]);
  const density = preferences.data?.value.density ?? DEFAULT_DENSITY;
  const setDensity = useCallback((next: LayoutDensity) => { queueUpdate<CommandCenterPreferencesValue>("command_center_preferences", null, (value) => ({ ...value, density: next })); }, [queueUpdate]);
  const value = useMemo<GraphState>(() => ({ expandedTaskIds, expandedFinishedIds, toggleExpanded, setExpandedTaskIds: replaceExpanded, density, setDensity, manualPositions, saveGraphPosition, clearGraphPositions }), [expandedTaskIds, expandedFinishedIds, toggleExpanded, replaceExpanded, density, setDensity, manualPositions, saveGraphPosition, clearGraphPositions]);
  return createElement(GraphStateContext.Provider, { value }, children);
}

export function useGraphState(): GraphState {
  const provided = useContext(GraphStateContext);
  const fallback = useFallbackState();
  return provided ?? fallback;
}

export function useExpandedTaskIds() {
  const { expandedTaskIds, expandedFinishedIds, toggleExpanded, setExpandedTaskIds } = useGraphState();
  return { expandedTaskIds, expandedFinishedIds, toggleExpanded, setExpandedTaskIds };
}
