/** Chat and aq chat share the command-backed session relay. */
import {
  getSessionMessagesApiSessionsNameMessagesGet,
  postSessionMessageApiSessionsNameMessagePost,
  type SessionMessagesResponse,
  type SessionMessageResponse,
} from "./client";

export type ChatMessagesResponse = SessionMessagesResponse;
export type SendChatMessageResponse = SessionMessageResponse;

export async function fetchChatMessages(
  projectId: string,
  opts: { since?: number; limit?: number; threadId?: string; sessionAddress?: string; allThreads?: boolean } = {},
): Promise<ChatMessagesResponse> {
  return (await getSessionMessagesApiSessionsNameMessagesGet({
    path: { name: opts.sessionAddress ?? `supervisor-${projectId}` },
    query: { since: opts.since, limit: opts.limit, thread_id: opts.threadId,
      supervisor_only: opts.allThreads },
    throwOnError: true,
  })).data;
}

export async function sendChatMessage(
  projectId: string,
  body: string,
  opts: { from?: string; threadId?: string; sessionAddress?: string } = {},
): Promise<SendChatMessageResponse> {
  return (await postSessionMessageApiSessionsNameMessagePost({
    path: { name: opts.sessionAddress ?? `supervisor-${projectId}` },
    body: { body, from: opts.from ?? "dashboard", from_kind: "user", thread_id: opts.threadId },
    throwOnError: true,
  })).data;
}
