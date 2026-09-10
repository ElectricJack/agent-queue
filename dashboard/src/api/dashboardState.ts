import { useQuery } from "@tanstack/react-query";
import { dashboardStateGet, dashboardStatePut, type DashboardStateDocumentResponse } from "./client";
import { client } from "./client";

export type DashboardNamespace =
  | "command_center_preferences"
  | "command_center_project_view"
  | "playbook_graph_view";

export interface ManualPosition { x: number; y: number }

export interface CommandCenterPreferencesValue {
  density?: "compact" | "comfortable" | "spacious";
}

export interface CommandCenterProjectViewValue {
  expanded_task_ids?: string[];
  expanded_finished_task_ids?: string[];
  manual_positions?: Record<string, ManualPosition>;
}

export interface PlaybookGraphViewValue {
  manual_positions?: Record<string, ManualPosition>;
}

export type DashboardValue =
  | CommandCenterPreferencesValue
  | CommandCenterProjectViewValue
  | PlaybookGraphViewValue;

export interface DashboardDocument<T extends DashboardValue = DashboardValue> {
  namespace: DashboardNamespace;
  subject: string | null;
  revision: number;
  exists: boolean;
  value: T;
}

export const dashboardDocumentKey = (namespace: DashboardNamespace, subject?: string | null) =>
  ["dashboard-state", namespace, subject ?? ""] as const;

export function defaultDashboardValue(namespace: "command_center_preferences"): CommandCenterPreferencesValue;
export function defaultDashboardValue(namespace: "command_center_project_view"): CommandCenterProjectViewValue;
export function defaultDashboardValue(namespace: "playbook_graph_view"): PlaybookGraphViewValue;
export function defaultDashboardValue(namespace: DashboardNamespace): DashboardValue {
  switch (namespace) {
    case "command_center_preferences": return { density: "comfortable" };
    case "command_center_project_view": return {
      expanded_task_ids: [], expanded_finished_task_ids: [], manual_positions: {},
    };
    case "playbook_graph_view": return { manual_positions: {} };
  }
}

export function defaultDashboardDocument<T extends DashboardValue>(
  namespace: DashboardNamespace,
  subject: string | null = null,
): DashboardDocument<T> {
  return {
    namespace,
    subject,
    revision: 0,
    exists: false,
    value: defaultDashboardValue(namespace as never) as T,
  };
}

export async function fetchDashboardDocument<T extends DashboardValue>(
  namespace: DashboardNamespace,
  subject: string | null = null,
): Promise<DashboardDocument<T>> {
  const response = await dashboardStateGet({
    client,
    body: { namespace, ...(subject ? { subject } : {}) },
    throwOnError: true,
  });
  return (response.data as DashboardStateDocumentResponse).document as unknown as DashboardDocument<T>;
}

export async function putDashboardDocument<T extends DashboardValue>(
  document: DashboardDocument<T>,
  value: T,
): Promise<DashboardDocument<T>> {
  const response = await dashboardStatePut({
    client,
    body: {
      namespace: document.namespace,
      ...(document.subject ? { subject: document.subject } : {}),
      // Project views and the playbook graph are CAS documents. Passing a
      // revision for the LWW density document is harmless and keeps the one
      // write path explicit about the version it read.
      base_revision: document.revision,
      value: value as Record<string, unknown>,
    },
    throwOnError: true,
  });
  return (response.data as DashboardStateDocumentResponse).document as unknown as DashboardDocument<T>;
}

/** The response interceptor preserves coded API errors for CAS recovery. */
export function conflictDocument(error: unknown): DashboardDocument | null {
  const payload = (error as { payload?: unknown } | null)?.payload;
  if (!payload || typeof payload !== "object") return null;
  const data = payload as { error_code?: unknown; current?: unknown };
  if (data.error_code !== "revision_conflict" || !data.current || typeof data.current !== "object") return null;
  return data.current as DashboardDocument;
}

export function useDashboardDocument<T extends DashboardValue>(
  namespace: DashboardNamespace,
  subject: string | null = null,
) {
  return useQuery({
    queryKey: dashboardDocumentKey(namespace, subject),
    queryFn: () => fetchDashboardDocument<T>(namespace, subject),
  });
}
