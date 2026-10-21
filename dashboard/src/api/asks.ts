import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  askList, askShow, askPrepareDecision, askDecide, askReply, askHistory,
  type AskListRequest, type AskPrepareDecisionRequest, type AskDecideRequest, type AskReplyRequest,
  type AskSourceRecord, type AskShowResponse, type AskListResponse,
} from "./client";

export type AskContext = {
  title?: string; why?: string; waiting_effect?: string;
  choices?: { key: string; label: string; effect: string }[];
  blocked_work?: { label: string; url: string }[];
  links?: { label: string; url: string }[];
  requested_by?: { label?: string; origin?: string };
};
export type Ask = AskSourceRecord & { context: AskContext };
export type AskDetailData = Omit<AskShowResponse, "ask"> & { ask: Ask };
export const askKeys = { all: ["asks"] as const, detail: (id: string) => ["asks", "detail", id] as const };

export function useAsks(filters: AskListRequest = {}) {
  return useQuery({
    queryKey: [...askKeys.all, "list", filters],
    queryFn: async () => (await askList({ body: filters, throwOnError: true })).data as
      Omit<AskListResponse, "items"> & { items: Ask[] },
    refetchInterval: 15_000,
  });
}
export function useAsk(id: string) {
  return useQuery({
    queryKey: askKeys.detail(id), enabled: Boolean(id),
    queryFn: async () => (await askShow({ body: { ask_id: id }, throwOnError: true })).data as AskDetailData,
    refetchInterval: 15_000,
  });
}
export function useAskHistory(id: string, cursor?: string) {
  return useQuery({ queryKey: [...askKeys.detail(id), "history", cursor], enabled: Boolean(id),
    queryFn: async () => (await askHistory({ body: { ask_id: id, cursor }, throwOnError: true })).data! });
}
export function usePrepareAsk() {
  return useMutation({ mutationFn: async (body: AskPrepareDecisionRequest) =>
    (await askPrepareDecision({ body, throwOnError: true })).data! });
}
export function useDecideAsk() {
  const client = useQueryClient();
  return useMutation({ mutationFn: async (body: AskDecideRequest) => {
    const result = (await askDecide({ body, throwOnError: true })).data!;
    if (!result.success) throw Object.assign(new Error(`Decision ${result.decision_id}: ${result.application_state}`), { payload: result });
    return result;
  }, onSettled: () => {
    void client.invalidateQueries({ queryKey: askKeys.all });
    void client.invalidateQueries({ queryKey: ["review"] });
    void client.invalidateQueries({ queryKey: ["reviews"] });
  } });
}
export function useReplyAsk() {
  const client = useQueryClient();
  return useMutation({ mutationFn: async (body: AskReplyRequest) =>
    (await askReply({ body, throwOnError: true })).data!,
  onSuccess: () => void client.invalidateQueries({ queryKey: askKeys.all }) });
}

export function useAskAlias(kind: "review" | "escalation", id: string) {
  return useQuery({ queryKey: [...askKeys.all, "alias", kind, id], enabled: Boolean(id),
    queryFn: async () => (await askShow({ body: { ask_id: id, alias_kind: kind }, throwOnError: true })).data as AskDetailData });
}
