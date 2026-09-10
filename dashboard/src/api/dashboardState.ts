import { useQuery, type QueryClient } from "@tanstack/react-query";
import {
  client,
  dashboardStateGet,
  dashboardStateList,
  dashboardStatePut,
  type CommandCenterPreferencesDocument,
  type CommandCenterProjectViewDocument,
  type DashboardStateDocumentResponse,
  type DashboardStateListResponse,
  type NavOrganizationDocument,
  type PlaybookGraphViewDocument,
  type ShellPreferencesDocument,
} from "./client";

export type DashboardStateDocument =
  | NavOrganizationDocument
  | ShellPreferencesDocument
  | CommandCenterPreferencesDocument
  | CommandCenterProjectViewDocument
  | PlaybookGraphViewDocument;

export type DashboardStateNamespace = DashboardStateDocument["namespace"];
export type DashboardNamespace = Extract<
  DashboardStateNamespace,
  "command_center_preferences" | "command_center_project_view" | "playbook_graph_view"
>;

/** Runtime metadata mirrors the server registry and lets the event path reject
 * an address whose claimed scope/subject shape does not match its namespace. */
export const DASHBOARD_STATE_NAMESPACES = {
  nav_organization: { scope: "workspace", subject: "none", writeMode: "cas" },
  shell_preferences: { scope: "user", subject: "none", writeMode: "lww" },
  command_center_preferences: { scope: "user", subject: "none", writeMode: "lww" },
  command_center_project_view: { scope: "user", subject: "project", writeMode: "cas" },
  playbook_graph_view: { scope: "user", subject: "none", writeMode: "cas" },
} as const satisfies Record<
  DashboardStateNamespace,
  { scope: "workspace" | "user"; subject: "none" | "project"; writeMode: "cas" | "lww" }
>;

export const DASHBOARD_STATE_BOOTSTRAP_KEY = ["dashboard-state"] as const;

export function dashboardStateDocumentKey(namespace: DashboardStateNamespace, subject?: string | null) {
  return ["dashboard-state", namespace, subject ?? ""] as const;
}

export const dashboardDocumentKey = (namespace: DashboardNamespace, subject?: string | null) =>
  dashboardStateDocumentKey(namespace, subject);

export function isDashboardStateNamespace(value: unknown): value is DashboardStateNamespace {
  return typeof value === "string" && value in DASHBOARD_STATE_NAMESPACES;
}

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

export async function fetchDashboardStateBootstrap(): Promise<DashboardStateListResponse> {
  const { data } = await dashboardStateList({ body: {}, throwOnError: true });
  return data as DashboardStateListResponse;
}

export async function fetchDashboardStateDocument(
  namespace: DashboardStateNamespace,
  subject?: string | null,
): Promise<DashboardStateDocument> {
  const { data } = await dashboardStateGet({
    body: { namespace, subject: subject ?? null },
    throwOnError: true,
  });
  return data.document as DashboardStateDocument;
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

/** Seed document queries from an authoritative bootstrap without allowing a
 * slow response to replace a newer mutation response or event-driven fetch. */
export function seedDashboardStateDocuments(
  queryClient: QueryClient,
  bootstrap: DashboardStateListResponse,
): void {
  for (const document of bootstrap.documents) {
    queryClient.setQueryData<DashboardStateDocument>(
      dashboardStateDocumentKey(document.namespace, document.subject),
      (current) => current == null || document.revision > current.revision ? document : current,
    );
  }
}
