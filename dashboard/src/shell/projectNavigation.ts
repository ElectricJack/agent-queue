import { matchPath } from "react-router-dom";

export const TASK_TABS = [
  { tab: "graph", label: "Graph" },
  { tab: "tasks", label: "Tasks" },
] as const;

export const PROJECT_TABS = [
  { tab: "overview", label: "Overview" },
  { tab: "sessions", label: "Sessions" },
  { tab: "workspaces", label: "Workspaces" },
  { tab: "profiles", label: "Profiles" },
  { tab: "playbooks", label: "Playbooks" },
  { tab: "config", label: "Config" },
] as const;

export type WorkspaceTab = (typeof TASK_TABS)[number]["tab"] | (typeof PROJECT_TABS)[number]["tab"];

export function isTaskTab(tab: WorkspaceTab): boolean {
  return tab === "graph" || tab === "tasks";
}

/** The URL is the single project selection; no tab-local or stored selection. */
export function projectNavigation(pathname: string): {
  projectId: string | null; tab: WorkspaceTab; isWorkspace: boolean;
} {
  const project = matchPath("/projects/:projectId/*", pathname);
  const global = matchPath("/command-center/*", pathname);
  const requestedTab = (project ?? global)?.params["*"]?.replace(/\/$/, "") || "graph";
  const tab = [...TASK_TABS, ...PROJECT_TABS].find((item) => item.tab === requestedTab)?.tab ?? "graph";
  let projectId = project?.params.projectId ?? null;
  if (projectId) {
    try { projectId = decodeURIComponent(projectId); }
    catch { /* Keep malformed legacy IDs inert rather than crashing navigation. */ }
  }
  return { projectId, tab, isWorkspace: !!(project || global) };
}

/** Resource tabs need a project; selecting All projects returns to the graph. */
export function workspaceHref(projectId: string | null | undefined, tab: WorkspaceTab, search = ""): string {
  const base = projectId ? `/projects/${encodeURIComponent(projectId)}` : "/command-center";
  const target = projectId || isTaskTab(tab) ? tab : "graph";
  return `${base}/${target}${search}`;
}
