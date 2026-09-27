import type { QueryClient } from "@tanstack/react-query";
import { matchPath } from "react-router-dom";
import { projectGraphQuery } from "./api/graph";
import { poolStatusQuery } from "./api/hooks";
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
  if (matchPath("/agents", pathname)) {
    // The pool directory is what the page shows first; without this its read
    // left with the shell's roster, sessions and review reads, queued behind
    // them for the browser's connection slots and the daemon's loop.
    return queryClient.prefetchQuery(poolStatusQuery());
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
