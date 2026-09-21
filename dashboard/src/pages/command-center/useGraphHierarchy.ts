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

/**
 * The graph view's own server-backed state: how dense the cards are drawn and
 * where the operator has pinned any of them.
 *
 * There is deliberately no expanded set here any more. The graph never
 * expands a container in place (operator decision 2026-09-20) -- you enter
 * one, which is addressable navigation and lives in the URL. The
 * `expanded_task_ids` / `expanded_finished_task_ids` / `expanded_initialised`
 * fields of the `command_center_project_view` document are still carried
 * through every write (other clients read them), but nothing here reads them,
 * so an expansion a viewer stored yesterday simply has no effect.
 */
interface GraphState {
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
let fallbackDensity: LayoutDensity = DEFAULT_DENSITY;
let fallbackPositions: ManualPositions = {};
let fallbackSnapshotValue: { density: LayoutDensity; manualPositions: ManualPositions } = {
  density: fallbackDensity, manualPositions: fallbackPositions,
};
const fallbackListeners = new Set<() => void>();
const notifyFallback = () => {
  fallbackSnapshotValue = { density: fallbackDensity, manualPositions: fallbackPositions };
  for (const listener of fallbackListeners) listener();
};

function fallbackSnapshot() { return fallbackSnapshotValue; }

/** Test helper: back to the server defaults, for consumers rendered without
 *  their route provider. */
export function resetGraphStateFallback() {
  fallbackDensity = DEFAULT_DENSITY;
  fallbackPositions = {};
  notifyFallback();
}

function useFallbackState(): GraphState {
  const snapshot = useSyncExternalStore(
    (listener) => { fallbackListeners.add(listener); return () => fallbackListeners.delete(listener); }, fallbackSnapshot,
  );
  return useMemo(() => ({
    density: snapshot.density,
    setDensity: (density) => { fallbackDensity = density; notifyFallback(); },
    manualPositions: snapshot.manualPositions,
    saveGraphPosition: (scope, id, position) => { fallbackPositions = { ...fallbackPositions, [scope]: { ...fallbackPositions[scope], [id]: position } }; notifyFallback(); },
    clearGraphPositions: (scope) => {
      const rest = { ...fallbackPositions };
      delete rest[scope];
      fallbackPositions = rest;
      notifyFallback();
    },
  }), [snapshot]);
}

/** Every field of the document, so a write of one never drops the others --
 *  including the expansion fields this module no longer reads. */
function projectValue(document: DashboardDocument<CommandCenterProjectViewValue>): Required<CommandCenterProjectViewValue> {
  return {
    expanded_task_ids: document.value.expanded_task_ids ?? [],
    expanded_finished_task_ids: document.value.expanded_finished_task_ids ?? [],
    manual_positions: document.value.manual_positions ?? {},
    expanded_initialised: document.value.expanded_initialised ?? false,
  };
}

/** Shared server state for graph density and manual positions. */
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
  const manualPositions = useMemo<ManualPositions>(() => {
    const result: ManualPositions = {};
    for (const [projectId, document] of documents) result[projectId] = projectValue(document).manual_positions;
    result[PLAYBOOK_POSITION_SCOPE] = playbooks.data?.value.manual_positions ?? {};
    return result;
  }, [documents, playbooks.data]);
  const updateProject = useCallback((projectId: string, change: (value: Required<CommandCenterProjectViewValue>) => CommandCenterProjectViewValue) => {
    if (projectId) queueUpdate<CommandCenterProjectViewValue>("command_center_project_view", projectId, (value) => change(projectValue({ ...defaultDashboardDocument<CommandCenterProjectViewValue>("command_center_project_view", projectId), value })));
  }, [queueUpdate]);

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
  const value = useMemo<GraphState>(() => ({
    density, setDensity, manualPositions, saveGraphPosition, clearGraphPositions,
  }), [density, setDensity, manualPositions, saveGraphPosition, clearGraphPositions]);
  return createElement(GraphStateContext.Provider, { value }, children);
}

export function useGraphState(): GraphState {
  const provided = useContext(GraphStateContext);
  const fallback = useFallbackState();
  return provided ?? fallback;
}
