/**
 * React Query hooks over a `KnowledgeAdapter`. Keys are `["knowledge", ...]`
 * so a landed write can stale every knowledge read at once; a conflict or a
 * refusal stales nothing because the server did not change.
 */
import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type UseInfiniteQueryResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import { describeKnowledgeError, type KnowledgeAdapter } from "./adapter";
import type {
  AsyncView,
  KnowledgeDetailView,
  KnowledgeDiffView,
  KnowledgeHistoryEntryView,
  KnowledgeHistoryPage,
  KnowledgeListFilters,
  KnowledgeListPage,
  KnowledgeUpdateInput,
  KnowledgeUpdateResult,
  TaskKnowledgeView,
} from "./model";

export const knowledgeKeys = {
  all: ["knowledge"] as const,
  lists: () => ["knowledge", "list"] as const,
  list: (filters: KnowledgeListFilters) => ["knowledge", "list", filters] as const,
  details: (recordId: string) => ["knowledge", "detail", recordId] as const,
  detail: (recordId: string, revisionId: string | null) => ["knowledge", "detail", recordId, revisionId] as const,
  history: (recordId: string) => ["knowledge", "history", recordId] as const,
  diff: (recordId: string, from: string, to: string) => ["knowledge", "diff", recordId, from, to] as const,
  task: (taskId: string) => ["knowledge", "task", taskId] as const,
};

export function useKnowledgeList(adapter: KnowledgeAdapter, filters: KnowledgeListFilters) {
  return useInfiniteQuery({
    queryKey: [...knowledgeKeys.list(filters), adapter.cacheKey ?? "fixture"],
    queryFn: ({ pageParam }) => adapter.list(filters, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.nextCursor,
  });
}

export function useKnowledgeDetail(adapter: KnowledgeAdapter, recordId: string | null, revisionId: string | null) {
  return useQuery({
    queryKey: [...knowledgeKeys.detail(recordId ?? "", revisionId), adapter.cacheKey ?? "fixture"],
    queryFn: () => adapter.show(recordId!, revisionId),
    enabled: recordId !== null,
  });
}

export function useKnowledgeHistory(adapter: KnowledgeAdapter, recordId: string, enabled = true) {
  return useInfiniteQuery({
    queryKey: [...knowledgeKeys.history(recordId), adapter.cacheKey ?? "fixture"],
    queryFn: ({ pageParam }) => adapter.history(recordId, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.nextCursor,
    enabled,
  });
}

export function useKnowledgeDiff(adapter: KnowledgeAdapter, recordId: string, from: string | null, to: string | null) {
  return useQuery({
    queryKey: [...knowledgeKeys.diff(recordId, from ?? "", to ?? ""), adapter.cacheKey ?? "fixture"],
    queryFn: () => adapter.diff(recordId, from!, to!),
    enabled: from !== null && to !== null,
  });
}

export function useTaskKnowledge(adapter: KnowledgeAdapter, taskId: string) {
  return useQuery({
    queryKey: [...knowledgeKeys.task(taskId), adapter.cacheKey ?? "fixture"],
    queryFn: () => adapter.taskKnowledge(taskId),
  });
}

export function useKnowledgeUpdate(adapter: KnowledgeAdapter) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: KnowledgeUpdateInput): Promise<KnowledgeUpdateResult> => adapter.update(input),
    onSuccess: (result, input) => {
      if (result.outcome !== "updated" && result.outcome !== "replayed" && result.outcome !== "unchanged") return;
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.lists() });
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.details(input.recordId) });
      void queryClient.invalidateQueries({ queryKey: knowledgeKeys.history(input.recordId) });
      void queryClient.invalidateQueries({ queryKey: ["knowledge", "task"] });
      void queryClient.invalidateQueries({ queryKey: ["records"] });
    },
  });
}

export function detailView(query: UseQueryResult<KnowledgeDetailView>): AsyncView<KnowledgeDetailView> {
  return queryView(query);
}

export function diffView(query: UseQueryResult<KnowledgeDiffView>): AsyncView<KnowledgeDiffView> {
  return queryView(query);
}

export function taskKnowledgeView(query: UseQueryResult<TaskKnowledgeView>): AsyncView<TaskKnowledgeView> {
  return queryView(query);
}

function queryView<T>(query: UseQueryResult<T>): AsyncView<T> {
  if (query.error) return { status: "error", message: describeKnowledgeError(query.error) };
  if (query.data === undefined) return { status: "loading" };
  return { status: "ready", data: query.data };
}

export function listView(
  query: UseInfiniteQueryResult<InfiniteData<KnowledgeListPage>>,
): AsyncView<KnowledgeListPage> {
  if (query.error) return { status: "error", message: describeKnowledgeError(query.error) };
  if (!query.data) return { status: "loading" };
  const pages = query.data.pages;
  return {
    status: "ready",
    data: {
      items: pages.flatMap((page) => page.items),
      nextCursor: pages[pages.length - 1]?.nextCursor ?? null,
    },
  };
}

export function historyView(
  query: UseInfiniteQueryResult<InfiniteData<KnowledgeHistoryPage>>,
): AsyncView<{ entries: KnowledgeHistoryEntryView[]; nextCursor: string | null }> {
  if (query.error) return { status: "error", message: describeKnowledgeError(query.error) };
  if (!query.data) return { status: "loading" };
  const pages = query.data.pages;
  return {
    status: "ready",
    data: {
      entries: pages.flatMap((page) => page.entries),
      nextCursor: pages[pages.length - 1]?.nextCursor ?? null,
    },
  };
}
