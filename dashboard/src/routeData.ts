import type { QueryClient } from "@tanstack/react-query";
import { matchPath } from "react-router-dom";
import { projectGraphQuery } from "./api/graph";
import { metricsSeriesQuery } from "./api/metrics";

/**
 * Start the cold page's read before its lazy shell mounts and fills the
 * browser's connection slots with roster/sidebar reads. The mounted hook
 * shares this request and cache entry, including cancellation and retries.
 * Prefetch errors stay on the query for the page to handle normally.
 */
export function prefetchInitialRoute(queryClient: QueryClient, pathname: string): Promise<void> {
  if (matchPath("/metrics", pathname)) {
    return queryClient.prefetchQuery(metricsSeriesQuery("1h"));
  }
  const project = matchPath("/projects/:projectId/tasks", pathname);
  if (project?.params.projectId) {
    // React Router decodes the same parameter when the workspace mounts.
    let projectId: string;
    try { projectId = decodeURIComponent(project.params.projectId); }
    catch { return Promise.resolve(); }
    return queryClient.prefetchQuery(projectGraphQuery(projectId));
  }
  return Promise.resolve();
}
