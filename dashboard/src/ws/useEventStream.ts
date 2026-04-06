/**
 * Hook that connects to the /ws/events WebSocket and dispatches
 * incoming notify.* events to the TanStack Query cache.
 *
 * - State-change events (task_started, task_completed, etc.) invalidate
 *   the relevant query keys so TanStack Query refetches.
 * - task_message events are dispatched via a callback for the live log.
 * - Reconnects with exponential backoff on disconnect.
 */

import { useEffect, useRef, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { NotifyEvent, TaskMessageEvent } from "./types";

const BASE_RECONNECT_MS = 1_000;
const MAX_RECONNECT_MS = 30_000;

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

interface UseEventStreamOptions {
  onTaskMessage?: (event: TaskMessageEvent) => void;
  onStatusChange?: (status: ConnectionStatus) => void;
}

export function useEventStream(options: UseEventStreamOptions = {}) {
  const queryClient = useQueryClient();
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectDelay = useRef(BASE_RECONNECT_MS);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const unmounted = useRef(false);

  const { onTaskMessage, onStatusChange } = options;

  const dispatchEvent = useCallback(
    (event: NotifyEvent) => {
      const type = event.event_type;

      switch (type) {
        // Task lifecycle — invalidate task + agent queries
        case "notify.task_started":
        case "notify.task_completed":
        case "notify.task_failed":
        case "notify.task_blocked":
        case "notify.task_stopped":
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          queryClient.invalidateQueries({ queryKey: ["task", event.task.id] });
          queryClient.invalidateQueries({ queryKey: ["agents"] });
          break;

        // Interaction — invalidate the specific task
        case "notify.agent_question":
        case "notify.plan_awaiting_approval":
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          queryClient.invalidateQueries({ queryKey: ["task", event.task.id] });
          break;

        // VCS events
        case "notify.pr_created":
        case "notify.merge_conflict":
        case "notify.push_failed":
          queryClient.invalidateQueries({ queryKey: ["task", event.task.id] });
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          break;

        // Budget / system
        case "notify.budget_warning":
          queryClient.invalidateQueries({ queryKey: ["system"] });
          break;

        case "notify.system_online":
          queryClient.invalidateQueries({ queryKey: ["health"] });
          queryClient.invalidateQueries({ queryKey: ["system"] });
          break;

        // Streaming — forward to callback, don't invalidate
        case "notify.task_message":
          onTaskMessage?.(event as TaskMessageEvent);
          break;

        // Thread open/close — invalidate tasks to pick up status changes
        case "notify.task_thread_open":
        case "notify.task_thread_close":
          break;

        // Chain stuck
        case "notify.chain_stuck":
        case "notify.stuck_defined_task":
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          break;

        // Text notifications — no cache impact
        case "notify.text":
          break;
      }
    },
    [queryClient, onTaskMessage],
  );

  const connect = useCallback(() => {
    if (unmounted.current) return;

    const wsBase = import.meta.env.VITE_WS_URL
      || `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
    const url = `${wsBase}/ws/events`;

    onStatusChange?.("connecting");
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      reconnectDelay.current = BASE_RECONNECT_MS;
      onStatusChange?.("connected");
    };

    ws.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data) as NotifyEvent;
        dispatchEvent(event);
      } catch {
        // Ignore malformed messages
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
      onStatusChange?.("disconnected");

      if (!unmounted.current) {
        reconnectTimer.current = setTimeout(() => {
          reconnectDelay.current = Math.min(
            reconnectDelay.current * 2,
            MAX_RECONNECT_MS,
          );
          connect();
        }, reconnectDelay.current);
      }
    };

    ws.onerror = () => {
      // onclose will fire after this — reconnect handled there
    };
  }, [dispatchEvent, onStatusChange]);

  useEffect(() => {
    unmounted.current = false;
    connect();

    return () => {
      unmounted.current = true;
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);
}
