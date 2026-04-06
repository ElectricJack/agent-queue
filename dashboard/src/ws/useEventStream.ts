/**
 * Singleton WebSocket connection to /ws/events.
 *
 * The connection lives at module scope — React components subscribe
 * to it via the useEventStream hook but never own its lifecycle.
 * Reconnects with exponential backoff.
 */

import { useEffect, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { NotifyEvent, TaskMessageEvent } from "./types";

const BASE_RECONNECT_MS = 1_000;
const MAX_RECONNECT_MS = 30_000;

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

// --- Module-level singleton state ---

type Listener = (event: NotifyEvent) => void;
type StatusListener = (status: ConnectionStatus) => void;

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
let reconnectDelay = BASE_RECONNECT_MS;
let currentStatus: ConnectionStatus = "disconnected";

const eventListeners = new Set<Listener>();
const statusListeners = new Set<StatusListener>();

function setStatus(s: ConnectionStatus) {
  currentStatus = s;
  for (const fn of statusListeners) fn(s);
}

function connect() {
  if (ws?.readyState === WebSocket.OPEN || ws?.readyState === WebSocket.CONNECTING) {
    return;
  }

  const wsBase = import.meta.env.VITE_WS_URL
    || `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
  const url = `${wsBase}/ws/events`;

  setStatus("connecting");
  const sock = new WebSocket(url);
  ws = sock;

  sock.onopen = () => {
    reconnectDelay = BASE_RECONNECT_MS;
    setStatus("connected");
  };

  sock.onmessage = (msg) => {
    try {
      const event = JSON.parse(msg.data) as NotifyEvent;
      for (const fn of eventListeners) fn(event);
    } catch {
      // ignore
    }
  };

  sock.onclose = () => {
    ws = null;
    setStatus("disconnected");
    reconnectTimer = setTimeout(() => {
      reconnectDelay = Math.min(reconnectDelay * 2, MAX_RECONNECT_MS);
      connect();
    }, reconnectDelay);
  };

  sock.onerror = () => {
    // onclose fires after — reconnect handled there
  };
}

// Start immediately on module load
connect();

// --- React hook ---

interface UseEventStreamOptions {
  onTaskMessage?: (event: TaskMessageEvent) => void;
  onEvent?: (event: NotifyEvent) => void;
  onStatusChange?: (status: ConnectionStatus) => void;
}

export function useEventStream(options: UseEventStreamOptions = {}) {
  const queryClient = useQueryClient();
  const { onTaskMessage, onEvent, onStatusChange } = options;

  // Subscribe to status changes
  useEffect(() => {
    if (!onStatusChange) return;
    statusListeners.add(onStatusChange);
    // Fire current status immediately
    onStatusChange(currentStatus);
    return () => { statusListeners.delete(onStatusChange); };
  }, [onStatusChange]);

  // Subscribe to events
  const handleEvent = useCallback(
    (event: NotifyEvent) => {
      onEvent?.(event);

      const type = event.event_type;
      switch (type) {
        case "notify.task_started":
        case "notify.task_completed":
        case "notify.task_failed":
        case "notify.task_blocked":
        case "notify.task_stopped":
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          queryClient.invalidateQueries({ queryKey: ["task", event.task.id] });
          queryClient.invalidateQueries({ queryKey: ["agents"] });
          break;

        case "notify.agent_question":
        case "notify.plan_awaiting_approval":
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          queryClient.invalidateQueries({ queryKey: ["task", event.task.id] });
          break;

        case "notify.pr_created":
        case "notify.merge_conflict":
        case "notify.push_failed":
          queryClient.invalidateQueries({ queryKey: ["task", event.task.id] });
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          break;

        case "notify.budget_warning":
          queryClient.invalidateQueries({ queryKey: ["system"] });
          break;

        case "notify.system_online":
          queryClient.invalidateQueries({ queryKey: ["health"] });
          queryClient.invalidateQueries({ queryKey: ["system"] });
          break;

        case "notify.task_message":
          onTaskMessage?.(event as TaskMessageEvent);
          break;

        case "notify.task_thread_open":
        case "notify.task_thread_close":
          break;

        case "notify.chain_stuck":
        case "notify.stuck_defined_task":
          queryClient.invalidateQueries({ queryKey: ["tasks"] });
          break;

        case "notify.text":
          break;
      }
    },
    [queryClient, onTaskMessage, onEvent],
  );

  useEffect(() => {
    eventListeners.add(handleEvent);
    return () => { eventListeners.delete(handleEvent); };
  }, [handleEvent]);
}
