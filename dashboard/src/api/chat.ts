/**
 * Chat wire — talks to /api/sessions/supervisor-<pid>/(message[s]).
 * These endpoints are not part of the codegen client (path carries the
 * session name), so we call them by URL. The generated client's baseUrl
 * config is honored via the same env-var precedence.
 */

import type { MessageModel } from "./client";

function baseUrl(): string {
  return import.meta.env.VITE_API_URL || "";
}

function supervisorName(projectId: string): string {
  return `supervisor-${projectId}`;
}

async function throwing(resp: Response): Promise<Response> {
  if (resp.ok) return resp;
  let detail: string;
  try {
    const body = await resp.clone().json();
    detail =
      typeof body?.detail === "string"
        ? body.detail
        : typeof body?.error === "string"
          ? body.error
          : JSON.stringify(body);
  } catch {
    detail = await resp.clone().text();
  }
  throw new Error(`API ${resp.status}: ${detail}`);
}

export interface ChatMessagesResponse {
  success: boolean;
  session: string;
  project_id: string;
  count: number;
  messages: MessageModel[];
}

export async function fetchChatMessages(
  projectId: string,
  opts: {
    since?: number;
    limit?: number;
    threadId?: string;
    /** Full session address override; defaults to `supervisor-${projectId}`. */
    sessionAddress?: string;
  } = {},
): Promise<ChatMessagesResponse> {
  const params = new URLSearchParams();
  if (opts.since !== undefined) params.set("since", String(opts.since));
  if (opts.limit !== undefined) params.set("limit", String(opts.limit));
  if (opts.threadId) params.set("thread_id", opts.threadId);
  const session = opts.sessionAddress ?? supervisorName(projectId);
  const url = `${baseUrl()}/api/sessions/${encodeURIComponent(
    session,
  )}/messages${params.toString() ? `?${params}` : ""}`;
  const resp = await throwing(await fetch(url));
  return (await resp.json()) as ChatMessagesResponse;
}

export interface SendChatMessageResponse {
  success: boolean;
  message_id: string;
  state: string;
}

export async function sendChatMessage(
  projectId: string,
  body: string,
  opts: { from?: string; threadId?: string; sessionAddress?: string } = {},
): Promise<SendChatMessageResponse> {
  const session = opts.sessionAddress ?? supervisorName(projectId);
  const url = `${baseUrl()}/api/sessions/${encodeURIComponent(session)}/message`;
  const resp = await throwing(
    await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        body,
        from: opts.from ?? "dashboard",
        from_kind: "user",
        thread_id: opts.threadId,
      }),
    }),
  );
  return (await resp.json()) as SendChatMessageResponse;
}
