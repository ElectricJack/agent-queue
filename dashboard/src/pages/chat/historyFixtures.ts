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
    inputs: [], next_before: null, ...overrides,
  };
}

export function historyResult(conversations: ConversationHistoryRecord[] = [], nextBefore: number | null = null) {
  return {
    data: { success: true, conversations, next_before: nextBefore }, error: undefined,
    request: new Request("http://localhost/api/supervisor_inbox/history"), response: new Response(),
  };
}
