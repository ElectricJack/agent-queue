import { useInfiniteQuery, useQueries } from "@tanstack/react-query";
import { recordSearch, recordShow, type RecordEdge } from "./client";
import { object } from "../pages/knowledge/liveAdapter";
import type { KnowledgeListFilters } from "../pages/knowledge/model";

export type RecordKindFilter = "task" | "knowledge" | "all";
export type RecordFilters = KnowledgeListFilters & { kind: RecordKindFilter };
export type RecordNode = {
  recordId: string;
  title: string;
} & ({ kind: "task"; taskId: string; status: string; archived: boolean }
  | { kind: "knowledge"; revisionId: string; category: string; lifecycle: string; verification: string });
export type { RecordEdge };

const string = (value: unknown) => typeof value === "string" ? value : "";

function node(value: unknown): RecordNode | null {
  const row = object(value);
  const base = { recordId: string(row.record_id), title: string(row.title) };
  if (!base.recordId || !base.title) return null;
  if (row.kind === "task" && typeof row.task_id === "string") return {
    ...base, kind: "task", taskId: row.task_id, status: string(row.status), archived: row.archived === true,
  };
  if (row.kind === "knowledge") return {
    ...base, kind: "knowledge", revisionId: string(row.revision_id), category: string(row.category),
    lifecycle: string(row.lifecycle), verification: string(row.verification),
  };
  return null;
}

export function useRecordSearch(projectId: string, filters: RecordFilters, enabled: boolean) {
  return useInfiniteQuery({
    queryKey: ["records", "search", projectId, filters],
    enabled,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }) => {
      const { data } = await recordSearch({ body: {
        project_id: projectId, kind: filters.kind, query: filters.query, cursor: pageParam,
        category: filters.category || null, lifecycle: filters.lifecycle || null,
        verification: filters.verification || null, include_retired: filters.lifecycle !== "active",
        include_disputed: true, limit: 25,
      } });
      if (!data || data.success === false) throw new Error("Record search unavailable");
      return { items: (data.items ?? []).map(node).filter((item): item is RecordNode => item !== null),
        nextCursor: data.next_cursor ?? null };
    },
    getNextPageParam: (page) => page.nextCursor,
  });
}

/** Drawing data only: these edges never enter the task layout or its cache. */
export function useRecordEdges(projectId: string, nodes: RecordNode[], enabled: boolean) {
  return useQueries({ queries: nodes.map((item) => ({
    queryKey: ["records", "edges", projectId, item.recordId, item.kind === "knowledge" ? item.revisionId : null],
    enabled,
    queryFn: async () => {
      const { data } = await recordShow({ body: { project_id: projectId,
        identity: `record:${item.recordId}`, include_edges: true,
        revision_id: item.kind === "knowledge" ? item.revisionId : null } });
      if (!data || data.success === false) throw new Error("Record links unavailable");
      return data.edges ?? [];
    },
  })) });
}
