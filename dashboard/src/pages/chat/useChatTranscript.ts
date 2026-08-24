import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchChatMessages, sendChatMessage, type ChatMessagesResponse } from "../../api/chat";
import type { MessageModel } from "../../api/client";
import { useEventStream } from "../../ws/useEventStream";
import type { NotifyEvent } from "../../ws/types";

export function threadIdFor(projectId: string): string {
  return `dashboard:${projectId}`;
}

const EVENT_TYPES_IN_CHAT = new Set<string>([
  "notify.task_started",
  "notify.task_completed",
  "notify.task_failed",
  "gate.created",
  "gate.resolved",
  "notify.playbook_run_failed",
]);

/**
 * Events that count as "the supervisor is doing something" while we wait
 * for its reply. Rendered as activity chips inside the thinking bubble.
 */
const ACTIVITY_EVENT_TYPES = new Set<string>([
  "notify.task_started",
  "notify.task_completed",
  "notify.task_failed",
  "notify.task_blocked",
  "notify.task_message",
  "notify.playbook_run_started",
  "notify.playbook_run_completed",
  "notify.playbook_run_failed",
  "notify.text",
  "gate.created",
  "gate.resolved",
  "session.started",
  "session.exited",
  "session.adopted",
  "command.invoked",
]);

export type PendingMessage = MessageModel & {
  pending?: boolean;
  failed?: boolean;
  /** Server-assigned id for this message, once known (from the send response). */
  serverId?: string;
};

export type ActivityHint = {
  ts: number;
  label: string;
  eventType: string;
};

export type ThinkingState = {
  /** Unix seconds of the sent user message we're awaiting a reply for. */
  since: number;
  /** Live activity hints observed while waiting. Newest last. */
  activities: ActivityHint[];
};

export type TranscriptItem =
  | { kind: "message"; ts: number; msg: PendingMessage }
  | { kind: "event"; ts: number; event: NotifyEvent };

function summarizeActivity(event: NotifyEvent): string | null {
  const e = event as unknown as Record<string, unknown>;
  const short = (v: unknown, n = 40) =>
    typeof v === "string" ? (v.length > n ? v.slice(0, n) + "…" : v) : "";
  switch (event.event_type) {
    case "notify.task_started":
      return `Started task ${short(e.task_title ?? e.task_id ?? "", 48)}`;
    case "notify.task_completed":
      return `Completed task ${short(e.task_title ?? e.task_id ?? "", 48)}`;
    case "notify.task_failed":
      return `Task failed: ${short(e.task_title ?? e.task_id ?? "", 48)}`;
    case "notify.task_blocked":
      return `Task blocked: ${short(e.task_title ?? e.task_id ?? "", 48)}`;
    case "notify.task_message": {
      const msg = short(e.message ?? "", 80);
      return msg ? `${msg}` : "Streaming output…";
    }
    case "notify.playbook_run_started":
      return `Playbook started: ${short(e.playbook_id ?? "", 40)}`;
    case "notify.playbook_run_completed":
      return `Playbook completed: ${short(e.playbook_id ?? "", 40)}`;
    case "notify.playbook_run_failed":
      return `Playbook failed: ${short(e.playbook_id ?? "", 40)}`;
    case "notify.text":
      return short(e.message ?? e.text ?? "", 80) || null;
    case "gate.created":
      return `Gate opened: ${short(e.gate_type ?? "gate", 30)}`;
    case "gate.resolved":
      return `Gate resolved: ${short(e.gate_type ?? "gate", 30)}`;
    case "session.started":
      return `Session woke up`;
    case "session.exited":
      return `Session exited`;
    case "session.adopted":
      return `Session adopted`;
    case "command.invoked": {
      const cmd = short(e.command ?? "", 40) || "command";
      if (e.ok === false) {
        const err = short(e.error ?? "", 60);
        return err ? `invoked ${cmd} — failed: ${err}` : `invoked ${cmd} — failed`;
      }
      const summary = short(e.args_summary ?? "", 80);
      const dur = typeof e.duration_ms === "number" ? `${e.duration_ms}ms` : null;
      const detail = [summary, dur].filter(Boolean).join(", ");
      return detail ? `invoked ${cmd} (${detail})` : `invoked ${cmd}`;
    }
    default:
      return null;
  }
}

export interface ChatTranscriptOverrides {
  /** Full messaging address to send TO. Default: `supervisor-${projectId}`. */
  sessionAddress?: string;
  /** Override the thread id. Default: `dashboard:${projectId}`. */
  threadIdOverride?: string;
}

export function useChatTranscript(
  projectId: string,
  overrides: ChatTranscriptOverrides = {},
) {
  const qc = useQueryClient();
  const thread = overrides.threadIdOverride ?? threadIdFor(projectId);
  const toId = overrides.sessionAddress ?? `supervisor-${projectId}`;

  const hydrate = useQuery({
    queryKey: ["chat", "thread", projectId, thread, toId],
    queryFn: (): Promise<ChatMessagesResponse> =>
      fetchChatMessages(projectId, {
        threadId: thread,
        limit: 200,
        sessionAddress: overrides.sessionAddress,
      }),
    enabled: !!projectId || !!overrides.sessionAddress,
    staleTime: 15_000,
    // Surface a chat failure promptly. On the client default (retry: 3) the
    // backoff runs 1s + 2s + 4s, so a rejected session address sat on a blank
    // "connecting" pane for ~7s before the error text appeared.
    retry: 1,
    retryDelay: 250,
  });

  const [live, setLive] = useState<MessageModel[]>([]);
  const [pending, setPending] = useState<PendingMessage[]>([]);
  const [events, setEvents] = useState<Array<{ ts: number; event: NotifyEvent }>>([]);
  const [thinking, setThinking] = useState<ThinkingState | null>(null);
  const seenIds = useRef<Set<string>>(new Set());
  const thinkingRef = useRef<ThinkingState | null>(null);
  thinkingRef.current = thinking;

  // Reset per-project when projectId changes
  useEffect(() => {
    setLive([]);
    setPending([]);
    setEvents([]);
    setThinking(null);
    seenIds.current = new Set();
  }, [projectId]);

  useEventStream({
    onEvent: useCallback(
      (event: NotifyEvent) => {
        const evProjectId = (event as { project_id?: string | null }).project_id;
        if (evProjectId && evProjectId !== projectId) return;

        if (event.event_type === "message.sent") {
          if (event.thread_id !== thread) return;
          const msgId = event.message_id;
          if (seenIds.current.has(msgId)) return;
          seenIds.current.add(msgId);
          // Re-fetch to pull the full row (message.sent event doesn't carry body).
          qc.invalidateQueries({ queryKey: ["chat", "thread", projectId, thread] });
          // Any non-user side arriving in this thread clears the thinking state.
          // The most reliable discriminator is to_kind — user-sent messages go
          // to a session; supervisor replies come back to a user recipient.
          const toKind = (event as { to_kind?: string }).to_kind;
          if (toKind === "user") {
            setThinking(null);
          }
          return;
        }

        if (EVENT_TYPES_IN_CHAT.has(event.event_type)) {
          setEvents((prev) => [...prev.slice(-99), { ts: Date.now() / 1000, event }]);
        }

        // Live activity hints while a reply is awaited.
        if (thinkingRef.current && ACTIVITY_EVENT_TYPES.has(event.event_type)) {
          // Filter command.invoked chips to the supervisor's own tool calls.
          // The supervisor session is project-scoped and NOT bound to a task
          // (see SessionLens: elevated project-scoped tokens are minted with
          // task_id=None), whereas a task-agent session's command.invoked
          // frames carry the task_id. Skipping the task-scoped ones keeps
          // the bubble focused on what the supervisor is doing right now.
          if (event.event_type === "command.invoked") {
            const taskId = (event as { task_id?: string | null }).task_id;
            if (taskId) return;
          }
          const label = summarizeActivity(event);
          if (!label) return;
          setThinking((prev) =>
            prev
              ? {
                  ...prev,
                  activities: [
                    ...prev.activities.slice(-19),
                    { ts: Date.now() / 1000, label, eventType: event.event_type },
                  ],
                }
              : prev,
          );
        }
      },
      [projectId, thread, qc],
    ),
  });

  // Hydrated messages drive live; drop pending rows once their server-assigned id
  // (captured from the sendChatMessage response) shows up in a hydrated row. Fall
  // back to a body match for pending rows that haven't resolved a serverId yet.
  useEffect(() => {
    const rows = hydrate.data?.messages ?? [];
    setLive(rows);
    for (const r of rows) seenIds.current.add(r.id);
    setPending((prev) =>
      prev.filter(
        (p) =>
          !rows.some((r) =>
            p.serverId ? r.id === p.serverId : r.body === p.body && r.from_kind === "user",
          ),
      ),
    );
    // Clear thinking if hydration picked up a supervisor reply newer than the
    // waiting-since timestamp — handles the case where WS misses the frame.
    const since = thinkingRef.current?.since ?? 0;
    if (since > 0) {
      const gotReply = rows.some(
        (r) => r.from_kind !== "user" && (r.created_at ?? 0) >= since,
      );
      if (gotReply) setThinking(null);
    }
  }, [hydrate.data]);

  const [sendError, setSendError] = useState<unknown>(null);
  const [isSending, setIsSending] = useState(false);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const send = useCallback(
    async (body: string) => {
      const trimmed = body.trim();
      if (!trimmed) return;
      const now = Date.now() / 1000;
      const optimistic: PendingMessage = {
        id: `optimistic-${Date.now()}`,
        project_id: projectId,
        from_kind: "user",
        from_id: "dashboard",
        to_kind: "session",
        to_id: toId,
        thread_id: thread,
        body: trimmed,
        priority: 100,
        created_at: now,
        delivered_at: null,
        read_at: null,
        subject: null,
        archive_after_inject: false,
        archived_at: null,
        reply_to_id: null,
        via: null,
        pending: true,
      } as PendingMessage;
      setPending((prev) => [...prev, optimistic]);
      setIsSending(true);
      setSendError(null);
      setThinking({ since: now, activities: [] });
      try {
        const res = await sendChatMessage(projectId, trimmed, {
          threadId: thread,
          sessionAddress: overrides.sessionAddress,
        });
        if (!mountedRef.current) return;
        setPending((prev) =>
          prev.map((p) => (p.id === optimistic.id ? { ...p, serverId: res.message_id } : p)),
        );
        qc.invalidateQueries({ queryKey: ["chat", "thread", projectId, thread] });
      } catch (err) {
        if (!mountedRef.current) return;
        setSendError(err);
        setPending((prev) =>
          prev.map((p) => (p.id === optimistic.id ? { ...p, failed: true, pending: false } : p)),
        );
        setThinking(null);
      } finally {
        if (mountedRef.current) setIsSending(false);
      }
    },
    [projectId, thread, qc],
  );

  const items = useMemo<TranscriptItem[]>(() => {
    const msgItems: TranscriptItem[] = [
      ...live.map((m) => ({
        kind: "message" as const,
        ts: m.created_at ?? 0,
        msg: m as PendingMessage,
      })),
      ...pending.map((m) => ({
        kind: "message" as const,
        ts: m.created_at ?? 0,
        msg: m,
      })),
    ];
    const evItems: TranscriptItem[] = events.map((e) => ({
      kind: "event",
      ts: e.ts,
      event: e.event,
    }));
    return [...msgItems, ...evItems].sort((a, b) => a.ts - b.ts);
  }, [live, pending, events]);

  return {
    items,
    isLoading: hydrate.isLoading,
    error: hydrate.error,
    send,
    isSending,
    sendError,
    thinking,
  };
}
