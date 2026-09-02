/**
 * Singleton WebSocket connection to /ws/events.
 *
 * The connection lives at module scope — React components subscribe
 * to it via the useEventStream hook but never own its lifecycle.
 * Reconnects with exponential backoff.
 */

import { useEffect, useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { NotifyEvent, TaskMessageEvent, ProposalStatusChangedEvent } from "./types";

const BASE_RECONNECT_MS = 1_000;
const MAX_RECONNECT_MS = 30_000;

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

// --- Module-level singleton state ---

type Listener = (event: NotifyEvent) => void;
type StatusListener = (status: ConnectionStatus) => void;

let ws: WebSocket | null = null;
let reconnectDelay = BASE_RECONNECT_MS;
let currentStatus: ConnectionStatus = "disconnected";

const eventListeners = new Set<Listener>();
const statusListeners = new Set<StatusListener>();

/** Test-only: push a synthetic frame through the same listener set the
 *  real WebSocket's onmessage handler uses. Not used by production code. */
export function __dispatchEventForTests(event: NotifyEvent): void {
  for (const fn of eventListeners) fn(event);
}

/**
 * Subscribe to raw frames without the query-invalidation pass.
 *
 * The Metrics tab needs every ``metrics.tick`` at 1 Hz and invalidates
 * nothing — routing it through `useEventStream` would run the whole
 * invalidation switch sixty times a minute for a frame that touches no
 * React Query key.
 */
export function useRawEventSubscription(listener: (event: NotifyEvent) => void): void {
  useEffect(() => {
    eventListeners.add(listener);
    return () => {
      eventListeners.delete(listener);
    };
  }, [listener]);
}

function setStatus(s: ConnectionStatus) {
  currentStatus = s;
  for (const fn of statusListeners) fn(s);
}

const LAST_SEQ_KEY = "aq:ws:last_seq";
const EPOCH_KEY = "aq:ws:epoch";

function loadLastSeq(): number | null {
  try {
    const raw = localStorage.getItem(LAST_SEQ_KEY);
    if (raw == null) return null;
    const n = Number.parseInt(raw, 10);
    return Number.isFinite(n) ? n : null;
  } catch {
    return null;
  }
}

function saveLastSeq(seq: number): void {
  try {
    localStorage.setItem(LAST_SEQ_KEY, String(seq));
  } catch {
    /* ignore */
  }
}

function loadEpoch(): string | null {
  try {
    return localStorage.getItem(EPOCH_KEY);
  } catch {
    return null;
  }
}

function saveEpoch(epoch: string): void {
  try {
    localStorage.setItem(EPOCH_KEY, epoch);
  } catch {
    /* ignore */
  }
}

function clearStoredSeq(): void {
  try {
    localStorage.removeItem(LAST_SEQ_KEY);
  } catch {
    /* ignore */
  }
}

function connect() {
  if (ws?.readyState === WebSocket.OPEN || ws?.readyState === WebSocket.CONNECTING) {
    return;
  }

  const wsBase = import.meta.env.VITE_WS_URL
    || `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}`;
  const lastSeq = loadLastSeq();
  const sentAfterSeq = lastSeq != null;
  const qs = sentAfterSeq ? `?after_seq=${lastSeq}` : "";
  const url = `${wsBase}/ws/events${qs}`;

  setStatus("connecting");
  const sock = new WebSocket(url);
  ws = sock;

  sock.onopen = () => {
    reconnectDelay = BASE_RECONNECT_MS;
    setStatus("connected");
  };

  sock.onmessage = (msg) => {
    try {
      const frame = JSON.parse(msg.data) as { type?: string; epoch?: string } & NotifyEvent & { seq?: number | null };
      // Epoch guard: if the server has a different epoch (daemon restart with
      // a fresh DB), discard the stored seq so the next reconnect does not
      // send a stale after_seq.  If we actually sent an after_seq on this
      // connection the server's replay cursor is stuck at the stale value and
      // will dedup every live frame — force a reconnect immediately so the
      // next connection starts clean (no after_seq).  If no seq was sent,
      // simply save the new epoch and continue; no reconnect needed.
      if (frame.type === "hello") {
        const storedEpoch = loadEpoch();
        if (storedEpoch !== frame.epoch) {
          clearStoredSeq();
          if (frame.epoch) saveEpoch(frame.epoch);
          if (sentAfterSeq) {
            sock.close();
          }
        }
        return;
      }
      // Ordinary bus events and replay use _event_type; notify payloads also
      // carry event_type. Give every subscriber the same discriminator while
      // retaining unknown event payloads for forward-compatible consumers.
      const type = typeof frame._event_type === "string" && frame._event_type
        ? frame._event_type : frame.event_type;
      if (typeof type !== "string" || !type) return;
      const event = { ...frame, _event_type: type, event_type: type } as NotifyEvent;
      if (typeof frame.seq === "number") saveLastSeq(frame.seq);
      for (const fn of eventListeners) fn(event);
    } catch {
      // ignore
    }
  };

  sock.onclose = () => {
    ws = null;
    setStatus("disconnected");
    setTimeout(() => {
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

      // The metrics sampler ticks once a second and owns no query cache of
      // its own — the Metrics page subscribes to the raw frame directly.
      if (type.startsWith("metrics.")) return;

      if (type.startsWith("notify.playbook_run_") || type.startsWith("playbook.")) {
        // Let in-flight snapshots finish; subsequent polling also recovers missed frames.
        queryClient.invalidateQueries({ queryKey: ["playbooks"] }, { cancelRefetch: false });
        queryClient.invalidateQueries({ queryKey: ["playbook-runs"] }, { cancelRefetch: false });
      }

      // Flock metadata includes assignments and direct-child activity across projects.
      if (/^(agent|session|task|message)\./.test(type)) {
        queryClient.invalidateQueries({ queryKey: ["agents"] });
      }

      if (type.startsWith("pool.")) {
        queryClient.invalidateQueries({ queryKey: ["pools"] });
        queryClient.invalidateQueries({ queryKey: ["sessions", "pool"] });
        return;
      }

      // Prefix-based invalidation for the wave-4 event families (gate.*,
      // message.*, session.*, task.blocked/unblocked). Handled *before* the
      // notify.* switch so the union type stays simple.
      if (type === "gate.created" || type === "gate.resolved" || type === "gate.expired") {
        queryClient.invalidateQueries({ queryKey: ["gates"] });
        queryClient.invalidateQueries({ queryKey: ["gate"] });
        queryClient.invalidateQueries({ queryKey: ["tasks"] });
        queryClient.invalidateQueries({ queryKey: ["explain"] });
        return;
      }
      if (
        type === "message.sent" ||
        type === "message.delivered" ||
        type === "message.replied"
      ) {
        queryClient.invalidateQueries({ queryKey: ["chat"] });
        return;
      }
      if (
        type === "session.started" ||
        type === "session.exited" ||
        type === "session.adopted"
      ) {
        queryClient.invalidateQueries({ queryKey: ["sessions"] });
        const sid = (event as { session_id?: string }).session_id;
        if (sid) queryClient.invalidateQueries({ queryKey: ["session", sid] });
        return;
      }
      if (type.startsWith("task.")) {
        const tid = (event as { task_id?: string }).task_id;
        queryClient.invalidateQueries({ queryKey: ["tasks"] });
        if (tid) {
          queryClient.invalidateQueries({ queryKey: ["task", tid] });
          queryClient.invalidateQueries({ queryKey: ["explain", tid] });
        }
        return;
      }
      if (type === "proposal.status_changed") {
        const pid = (event as ProposalStatusChangedEvent).proposal_id;
        queryClient.invalidateQueries({ queryKey: ["proposal", pid] });
        return;
      }

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
