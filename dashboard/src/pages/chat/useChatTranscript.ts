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

export type PendingMessage = MessageModel & {
  pending?: boolean;
  failed?: boolean;
  /** Server-assigned id for this message, once known (from the send response). */
  serverId?: string;
};

export type TranscriptItem =
  | { kind: "message"; ts: number; msg: PendingMessage }
  | { kind: "event"; ts: number; event: NotifyEvent };

export function useChatTranscript(projectId: string) {
  const qc = useQueryClient();
  const thread = threadIdFor(projectId);

  const hydrate = useQuery({
    queryKey: ["chat", "thread", projectId, thread],
    queryFn: (): Promise<ChatMessagesResponse> =>
      fetchChatMessages(projectId, { threadId: thread, limit: 200 }),
    enabled: !!projectId,
    staleTime: 15_000,
  });

  const [live, setLive] = useState<MessageModel[]>([]);
  const [pending, setPending] = useState<PendingMessage[]>([]);
  const [events, setEvents] = useState<Array<{ ts: number; event: NotifyEvent }>>([]);
  const seenIds = useRef<Set<string>>(new Set());

  // Reset per-project when projectId changes
  useEffect(() => {
    setLive([]);
    setPending([]);
    setEvents([]);
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
          return;
        }

        if (EVENT_TYPES_IN_CHAT.has(event.event_type)) {
          setEvents((prev) => [...prev.slice(-99), { ts: Date.now() / 1000, event }]);
        }
      },
      [projectId, thread, qc],
    ),
  });

  // Hydrated messages drive live; drop pending rows once their server-assigned id
  // (captured from the sendChatMessage response) shows up in a hydrated row. Fall
  // back to a body match for pending rows that haven't resolved a serverId yet
  // (e.g. the send is still in flight when hydration races ahead).
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
  }, [hydrate.data]);

  const [sendError, setSendError] = useState<unknown>(null);
  const [isSending, setIsSending] = useState(false);

  const send = useCallback(
    async (body: string) => {
      const trimmed = body.trim();
      if (!trimmed) return;
      const optimistic: PendingMessage = {
        id: `optimistic-${Date.now()}`,
        project_id: projectId,
        from_kind: "user",
        from_id: "dashboard",
        to_kind: "session",
        to_id: `supervisor-${projectId}`,
        thread_id: thread,
        body: trimmed,
        priority: 100,
        created_at: Date.now() / 1000,
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
      try {
        const res = await sendChatMessage(projectId, trimmed, { threadId: thread });
        setPending((prev) =>
          prev.map((p) => (p.id === optimistic.id ? { ...p, serverId: res.message_id } : p)),
        );
        qc.invalidateQueries({ queryKey: ["chat", "thread", projectId, thread] });
      } catch (err) {
        setSendError(err);
        setPending((prev) =>
          prev.map((p) => (p.id === optimistic.id ? { ...p, failed: true, pending: false } : p)),
        );
      } finally {
        setIsSending(false);
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
  };
}
