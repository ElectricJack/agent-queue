import { useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { useQueries, type UseQueryResult } from "@tanstack/react-query";
import type { EpicDeliveryStatus, LayoutNode } from "@aq/ts-client";

import { fetchList, type ListParams } from "../../api/graphLayout";
import { useProjects } from "../../api/hooks";
import { referencesBatch } from "./focusFormat";
import { FocusError } from "./FocusNotice";
import { useFocusChrome } from "./focusChrome";
import { focusTaskHref } from "./routes";

/** One page of a project's flat node list; `LIST_CAP` is the server's limit. */
const LIST: ListParams = {
  variant: "all",
  expanded: [],
  q: "",
  status: "",
  cursor: null,
  limit: 200,
};

/** A batch as the graph publishes it: an epic whose delivery names this batch. */
export interface BatchMember {
  node: LayoutNode;
  delivery: EpicDeliveryStatus;
}

function projectListKey(projectId: string) {
  return ["focus-batch", projectId, LIST.variant, LIST.limit] as const;
}

function useBatchScan(batchId: string, projectIds: string[]) {
  const results: UseQueryResult<Awaited<ReturnType<typeof fetchList>>>[] = useQueries({
    queries: projectIds.map((projectId) => ({
      queryKey: projectListKey(projectId),
      queryFn: async ({ signal }: { signal: AbortSignal }) => fetchList(projectId, LIST, signal),
      staleTime: 15_000,
      refetchInterval: 30_000,
      retry: 1,
    })),
  });
  return useMemo(
    () =>
      results.flatMap((result, index) => {
        const nodes = result.data && !("pending" in result.data) ? (result.data.nodes ?? []) : [];
        return nodes
          .filter((node) => referencesBatch(node.delivery, batchId))
          .map((node) => ({
            projectId: projectIds[index],
            member: { node, delivery: node.delivery as EpicDeliveryStatus } satisfies BatchMember,
            children: nodes.filter((other) => other.container_id === node.id),
          }));
      }),
    // The nodes are new objects per fetch, so the identity of any node is a
    // fine signature; projectIds only changes when the project list does.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [results.map((result) => result.data), projectIds.join(",")],
  );
}

const SECTION = "rounded-lg border border-gray-800 p-3";

/**
 * `/focus/batches/:batchId` — the integration batch as a phone page: the
 * epic whose delivery names this batch, its delivery state, its pull request,
 * and the tasks it carries.
 *
 * The daemon publishes no batch row to the dashboard wire, so the page
 * resolves a batch from the one projection that names one: the epic delivery
 * status the graph already sends, whose `responsible` / `links` carry
 * `kind: "batch"`. That keeps the page on the generated client and gives a
 * truthful answer when a batch has no epic pointing at it.
 */
export default function FocusBatch() {
  const { batchId = "" } = useParams();
  return <FocusBatchContent key={batchId} batchId={batchId} />;
}

function FocusBatchContent({ batchId }: { batchId: string }) {
  const projects = useProjects();
  const projectIds = useMemo(() => (projects.data ?? []).map((project) => project.id), [projects.data]);
  const found = useBatchScan(batchId, projectIds);
  useFocusChrome({ title: `Batch ${batchId}`, fullHref: "/command-center/graph" });

  if (projects.isError) {
    return (
      <FocusError
        title="Batch"
        message={projects.error instanceof Error ? projects.error.message : "Projects could not be read."}
        onRetry={() => void projects.refetch()}
      />
    );
  }
  if (projects.isLoading || !projects.data) {
    return <p className="p-4 text-sm text-gray-500">Loading…</p>;
  }

  return (
    <div className="space-y-4 p-3">
      <header className="space-y-1">
        <h2 className="break-all font-mono text-sm text-gray-300">{batchId}</h2>
        <p className="text-xs text-gray-500">
          {found.length === 0
            ? "No project reports this integration batch."
            : `${found.length} collection${found.length === 1 ? "" : "s"} integrating under this batch.`}
        </p>
      </header>

      {found.map(({ projectId, member, children }) => {
        const { node, delivery } = member;
        return (
          <section key={`${projectId}/${node.id}`} className={SECTION}>
            <h3 className="text-sm font-semibold text-gray-200">{node.title}</h3>
            <p className="text-xs text-gray-500">
              {projectId} · {delivery.display_status}
              {delivery.state !== delivery.display_status.toLowerCase() && ` · ${delivery.state}`}
            </p>
            {delivery.reason && <p className="pt-1 text-sm text-gray-300">{delivery.reason}</p>}
            {delivery.remedy && <p className="text-sm text-gray-400">{delivery.remedy}</p>}
            {delivery.evidence && (
              <p className="text-xs text-gray-500">Evidence: {delivery.evidence}</p>
            )}
            {node.pr_url && (
              <a
                data-primary-control
                href={node.pr_url}
                rel="noreferrer"
                className="mt-2 inline-flex text-sm text-indigo-300"
              >
                Pull request
              </a>
            )}
            {children.length > 0 && (
              <ul className="mt-2 divide-y divide-gray-800">
                {children.map((child) => (
                  <li key={child.id}>
                    <Link to={focusTaskHref(child.id)} className="block py-1.5 text-sm text-gray-200">
                      {child.title}
                      <span className="block text-xs text-gray-500">{child.status.replace(/_/g, " ")}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </div>
  );
}