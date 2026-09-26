import type { ConversationHistoryRecord, ConversationInputRecord } from "../../api/client";

export function historyInput(receivedAt: number): ConversationInputRecord {
  return {
    id: `input-${receivedAt}`, conversation_id: "conv-one", verified_actor: "human:discord:111",
    text: "Hello", text_expired: false, state: "accepted", received_at: receivedAt,
    reply_message_id: null, reply_body: null, reply_created_at: null,
  };
}

export function historyConversation(overrides: Partial<ConversationHistoryRecord> = {}): ConversationHistoryRecord {
  return {
    id: "conv-one", transport: "discord", guild_id: "guild", channel_id: "channel",
    external_root_message_id: "root", external_thread_id: "external-thread",
    thread_id: "conversation:conv-one", created_by: "human:discord:111", audience: ["111"],
    state: "open", created_at: 100, updated_at: 100, closed_at: null,
    inputs: [], next_before: null, next_before_id: null, ...overrides,
  };
}

export function historyResult(
  conversations: ConversationHistoryRecord[] = [], nextBefore: number | null = null,
  nextBeforeId: string | null = null,
) {
  return {
    data: { success: true, conversations, next_before: nextBefore, next_before_id: nextBeforeId }, error: undefined,
    request: new Request("http://localhost/api/supervisor_inbox/history"), response: new Response(),
  };
}

export function statusResult(enabled: boolean) {
  return {
    data: {
      success: true, enabled, preconditions: { ok: enabled, unmet: [] },
      diagnostics: { message_content_intent: true, permissions: null, outbox_bound: true },
      limits: { max_input_chars: 4000, author_window_limit: 10, channel_window_limit: 60,
        window_seconds: 600, max_reply_chars: 1900 },
      counts: { by_state: {}, inputs_pending_supervisor: 0 },
      backfill: { cursors: [], gaps: [] },
      intake: { available: true, window_seconds: 3600, total: 0, ignored: {} },
    },
    error: undefined,
    request: new Request("http://localhost/api/supervisor_inbox/status"), response: new Response(),
  };
}
