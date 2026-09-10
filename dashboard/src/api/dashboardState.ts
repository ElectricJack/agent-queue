import type { QueryClient } from "@tanstack/react-query";
import {
  dashboardStateGet,
  dashboardStateList,
  type CommandCenterPreferencesDocument,
  type CommandCenterProjectViewDocument,
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

export function isDashboardStateNamespace(value: unknown): value is DashboardStateNamespace {
  return typeof value === "string" && value in DASHBOARD_STATE_NAMESPACES;
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

