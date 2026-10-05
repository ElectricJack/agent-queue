// The supervisor conversation reads the focus routes need.
//
// Both pages share one wire contract, so the request shape, the
// "disabled means no conversations" branch and the paging cursor live here
// rather than twice. `supervisor_inbox_history` is a generated command
// (`/api/supervisor_inbox/history`); it is the same read the desktop
// `/conversations` picker uses, so a focus link and a desktop link name the
// same conversation.
import { useQuery } from "@tanstack/react-query";

import {
  supervisorInboxHistory,
  supervisorInboxStatus,
  type ConversationHistoryRecord,
  type SupervisorInboxHistoryResponse,
} from "@aq/ts-client";

export type { ConversationHistoryRecord };

/** `(before, before_id)` pages rows that share a timestamp without skipping any. */
export type HistoryCursor = { before: number; before_id: string | null };

export const historyKey = (conversationId: string | null) =>
  ["focus-conversations", conversationId ?? "all"] as const;

/** Whether the conversation route is live at all; false renders the reason. */
export function useConversationEnabled() {
  return useQuery({
    queryKey: ["supervisor-inbox", "status"],
    queryFn: async () => {
      const { data } = await supervisorInboxStatus({ body: {}, throwOnError: true });
      if (!data || !data.success || !("enabled" in data)) {
        throw new Error(
          data && "error" in data && typeof data.error === "string"
            ? data.error
            : "Failed to load conversation status",
        );
      }
      return data;
    },
    staleTime: 15_000,
    retry: 1,
  });
}

/** One conversation's history, or the newest page of every conversation. */
export function useConversationHistory(conversationId: string | null, pageParam?: HistoryCursor) {
  return useQuery({
    queryKey: [...historyKey(conversationId), pageParam ?? null],
    queryFn: async () => {
      const { data } = await supervisorInboxHistory({
        body: {
          limit: 50,
          ...(conversationId ? { conversation_id: conversationId } : {}),
          ...(pageParam ? { before: pageParam.before } : {}),
          ...(pageParam?.before_id == null ? {} : { before_id: pageParam.before_id }),
        },
        throwOnError: true,
      });
      if (!data || !data.success || !("conversations" in data)) {
        throw new Error(
          data && "error" in data && typeof data.error === "string"
            ? data.error
            : "Failed to load conversations",
        );
      }
      return data as SupervisorInboxHistoryResponse;
    },
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: 1,
  });
}

/** The newest input in a conversation, as epoch seconds; 0 when it has none. */
export function lastInputAt(conversation: ConversationHistoryRecord): number {
  return Math.max(0, ...conversation.inputs.map((input) => input.received_at));
}

/** Discord turns are attributed by the author's user id; name it as we can. */
export function conversationLabel(conversation: ConversationHistoryRecord): string {
  const prefix = "human:discord:";
  return conversation.created_by.startsWith(prefix)
    ? `Discord · ${conversation.created_by.slice(prefix.length)}`
    : conversation.created_by;
}

/** The addressable id of a conversation: its thread id carries the prefix. */
export function conversationThreadId(conversationId: string): string {
  return `conversation:${conversationId}`;
}