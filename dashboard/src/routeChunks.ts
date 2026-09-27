import { matchPath } from "react-router-dom";
import { isFocusPath } from "./pages/focus/routes";

/**
 * Loaders for the route chunks a session is most likely to need next. App.tsx
 * builds its lazy routes from them; main.tsx calls them once the first screen
 * is up, so switching to the project workspace does not wait on fetching and
 * compiling the graph view (React Flow included) or the task list.
 */
export const loadAppShell = () => import("./shell/AppShellV2");
export const loadCommandCenter = () => import("./pages/CommandCenter");
export const loadAgentWorkspace = () => import("./pages/agents/AgentWorkspace");
export const loadMetrics = () => import("./pages/metrics/Metrics");
export const loadWorkspaceGraph = () => import("./pages/command-center/Graph");
export const loadWorkspaceTasks = () => import("./pages/command-center/Tasks");

export function preloadWorkspaceViews(): void {
  void loadWorkspaceGraph();
  void loadWorkspaceTasks();
}

/**
 * Start every lazy chunk the first screen renders through, together.
 *
 * The routes nest lazily: the shell, then the project workspace, then its tab.
 * Left to React, each chunk is only requested once its parent has downloaded,
 * compiled and rendered, and each Suspense reveal is held back until 300 ms
 * after the previous fallback — three sequential round trips and two throttled
 * reveals before a cold Tasks page could show a row. Requested up front they
 * download in parallel, and each lazy route finds its module already loading.
 */
export function preloadInitialRouteChunks(pathname: string): void {
  if (isFocusPath(pathname)) return;
  void loadAppShell();
  if (matchPath("/agents", pathname)) {
    void loadAgentWorkspace();
    return;
  }
  if (matchPath("/metrics", pathname)) {
    void loadMetrics();
    return;
  }
  const tab = matchPath("/projects/:projectId/:tab", pathname)?.params.tab;
  if (tab === "tasks" || tab === "graph") {
    void loadCommandCenter();
    void (tab === "tasks" ? loadWorkspaceTasks() : loadWorkspaceGraph());
  }
}
