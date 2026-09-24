/**
 * Loaders for the route chunks a session is most likely to need next. App.tsx
 * builds its lazy routes from them; main.tsx calls them once the first screen
 * is up, so switching to the project workspace does not wait on fetching and
 * compiling the graph view (React Flow included) or the task list.
 */
export const loadWorkspaceGraph = () => import("./pages/command-center/Graph");
export const loadWorkspaceTasks = () => import("./pages/command-center/Tasks");

export function preloadWorkspaceViews(): void {
  void loadWorkspaceGraph();
  void loadWorkspaceTasks();
}
