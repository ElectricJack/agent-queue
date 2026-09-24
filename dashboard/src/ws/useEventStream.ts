/**
 * Singleton WebSocket connection to /ws/events.
 *
 * The connection lives at module scope — React components subscribe
 * to it via the useEventStream hook but never own its lifecycle.
 * Reconnects with exponential backoff.
 */

import { useEffect, useCallback } from "react";
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import {
  DASHBOARD_STATE_BOOTSTRAP_KEY,
  DASHBOARD_STATE_NAMESPACES,
  dashboardStateDocumentKey,
  isDashboardStateNamespace,
  type DashboardStateDocument,
} from "../api/dashboardState";
import type { DashboardStateListResponse } from "../api/client";
import type { NotifyEvent, TaskMessageEvent, ProposalStatusChangedEvent } from "./types";
import { readDeviceLocal, removeDeviceLocal, writeDeviceLocal } from "../deviceLocal";

const BASE_RECONNECT_MS = 1_000;
/** Window over which a burst of playbook frames collapses into one refetch. */
const PLAYBOOK_INVALIDATE_MS = 400;
/** Dashboard document bursts coalesce independently by QueryClient + address. */
const DASHBOARD_STATE_INVALIDATE_MS = 100;
const MAX_RECONNECT_MS = 30_000;

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

// --- Module-level singleton state ---

type Listener = (event: NotifyEvent) => void;
type StatusListener = (status: ConnectionStatus) => void;

let ws: WebSocket | null = null;
let reconnectDelay = BASE_RECONNECT_MS;
let currentStatus: ConnectionStatus = "disconnected";
let connectedGeneration = 0;

let playbookInvalidation: ReturnType<typeof setTimeout> | null = null;

const dashboardStateInvalidations = new WeakMap<
  QueryClient,
  Map<string, ReturnType<typeof setTimeout>>
>();
const dashboardStateBootstrapGeneration = new WeakMap<QueryClient, number>();

const eventListeners = new Set<Listener>();
const statusListeners = new Set<StatusListener>();

/** Test-only: push a synthetic frame through the same listener set the
 *  real WebSocket's onmessage handler uses. Not used by production code. */
export function __dispatchEventForTests(event: NotifyEvent): void {
  for (const fn of eventListeners) fn(event);
}

/** Test-only connection transition for proving bootstrap gap recovery. */
export function __setConnectionStatusForTests(status: ConnectionStatus): void {
  setStatus(status);
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
  if (s === "connected" && currentStatus !== "connected") connectedGeneration += 1;
  currentStatus = s;
  for (const fn of statusListeners) fn(s);
}

interface DashboardStateChange {
  version: 1;
  scope: "workspace" | "user";
  ownerId: string;
  namespace: keyof typeof DASHBOARD_STATE_NAMESPACES;
  subject: string | null;
  revision: number;
}

function dashboardStateChange(event: NotifyEvent): DashboardStateChange | null {
  const outer = event as unknown as Record<string, unknown>;
  let nested: unknown = outer.payload;
  if (typeof nested === "string") {
    try {
      nested = JSON.parse(nested);
    } catch {
      return null;
    }
  }
  const raw = nested != null && typeof nested === "object" && !Array.isArray(nested)
    ? { ...outer, ...(nested as Record<string, unknown>) }
    : outer;
  if (
    raw.version !== 1
    || (raw.scope !== "workspace" && raw.scope !== "user")
    || typeof raw.owner_id !== "string"
    || !isDashboardStateNamespace(raw.namespace)
    || (raw.subject !== null && typeof raw.subject !== "string")
    || !Number.isSafeInteger(raw.revision)
    || (raw.revision as number) < 0
  ) return null;

  const metadata = DASHBOARD_STATE_NAMESPACES[raw.namespace];
  if (metadata.scope !== raw.scope) return null;
  if (metadata.subject === "project" ? !raw.subject : raw.subject !== null) return null;
  if (raw.scope === "workspace" ? raw.owner_id !== "" : !raw.owner_id) return null;

  return {
    version: 1,
    scope: raw.scope,
    ownerId: raw.owner_id,
    namespace: raw.namespace,
    subject: raw.subject,
    revision: raw.revision as number,
  };
}

function scheduleDashboardStateInvalidation(
  queryClient: QueryClient,
  change: DashboardStateChange,
): void {
  const bootstrap = queryClient.getQueryData<DashboardStateListResponse>(
    DASHBOARD_STATE_BOOTSTRAP_KEY,
  );
  if (change.ownerId !== "" && change.ownerId !== bootstrap?.owner_id) return;

  const queryKey = dashboardStateDocumentKey(change.namespace, change.subject);
  const cached = queryClient.getQueryData<DashboardStateDocument>(queryKey);
  const bootstrapped = bootstrap?.documents.find(
    (document) => document.namespace === change.namespace
      && (document.subject ?? "") === (change.subject ?? ""),
  );
  const heldRevision = Math.max(cached?.revision ?? 0, bootstrapped?.revision ?? 0);
  if (change.revision <= heldRevision) return;

  let pending = dashboardStateInvalidations.get(queryClient);
  if (!pending) {
    pending = new Map();
    dashboardStateInvalidations.set(queryClient, pending);
  }
  const address = JSON.stringify(queryKey);
  if (pending.has(address)) return;
  pending.set(address, setTimeout(() => {
    pending?.delete(address);
    void queryClient.invalidateQueries(
      { queryKey, exact: true },
      { cancelRefetch: false },
    );
  }, DASHBOARD_STATE_INVALIDATE_MS));
}

/**
 * Device-local transport cursors (see `../deviceLocal`): they describe this
 * browser's WebSocket replay position, are never sent to the dashboard-state
 * API, and must never become roaming user preferences.
 */
function loadLastSeq(): number | null {
  const raw = readDeviceLocal("aq:ws:last_seq");
  if (raw == null) return null;
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) ? n : null;
}

function saveLastSeq(seq: number): void {
  writeDeviceLocal("aq:ws:last_seq", String(seq));
}

function loadEpoch(): string | null {
  return readDeviceLocal("aq:ws:epoch");
}

function saveEpoch(epoch: string): void {
  writeDeviceLocal("aq:ws:epoch", epoch);
}

function clearStoredSeq(): void {
  removeDeviceLocal("aq:ws:last_seq");
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

// --- Query-cache pass ---

/** Window over which a burst of frames collapses into one roster / pool refetch. */
const ROSTER_INVALIDATE_MS = 1_000;

const coalescedInvalidations = new WeakMap<
  QueryClient,
  Map<string, ReturnType<typeof setTimeout>>
>();

/**
 * Invalidate `queryKey` once at the end of a `delayMs` window opened by the
 * first frame; frames arriving inside the window ride along. The refetch
 * therefore always starts after the last frame of a burst, so it cannot miss
 * the change that frame announced.
 */
function scheduleCoalescedInvalidation(
  queryClient: QueryClient,
  queryKey: readonly unknown[],
  delayMs: number,
): void {
  let pending = coalescedInvalidations.get(queryClient);
  if (!pending) {
    pending = new Map();
    coalescedInvalidations.set(queryClient, pending);
  }
  const address = JSON.stringify(queryKey);
  if (pending.has(address)) return;
  pending.set(address, setTimeout(() => {
    pending?.delete(address);
    void queryClient.invalidateQueries({ queryKey });
  }, delayMs));
}

/**
 * Apply one frame to a QueryClient's cache. Runs once per frame per client —
 * not once per mounted `useEventStream` — because several of them are always
 * mounted (the root provider, the shell's agent-push bridge, the project
 * graph, the activity drawer, open panes) and each repeat re-issued the same
 * invalidations: with React Query's default `cancelRefetch` the second one
 * restarts the first's refetch, and since the fetchers ignore the abort
 * signal every restart was one more request to the daemon.
 */
function applyEventToCache(queryClient: QueryClient, event: NotifyEvent): void {
  const type = event.event_type;

  // The metrics sampler ticks once a second and owns no query cache of
  // its own — the Metrics page subscribes to the raw frame directly.
  if (type.startsWith("metrics.")) return;

  if (type === "dashboard_state.changed.v1") {
    const change = dashboardStateChange(event);
    if (change) scheduleDashboardStateInvalidation(queryClient, change);
    return;
  }

  if (type.startsWith("notify.playbook_run_") || type.startsWith("playbook.")) {
    // Coalesced: one run emits a frame per step, and refetching both
    // lists on each of them would turn a ten-step playbook into twenty
    // requests. Let in-flight snapshots finish; polling also recovers
    // any frame missed while disconnected.
    if (playbookInvalidation == null) {
      playbookInvalidation = setTimeout(() => {
        playbookInvalidation = null;
        queryClient.invalidateQueries({ queryKey: ["playbooks"] }, { cancelRefetch: false });
        queryClient.invalidateQueries({ queryKey: ["playbook-runs"] }, { cancelRefetch: false });
      }, PLAYBOOK_INVALIDATE_MS);
    }
  }

  // Flock metadata includes assignments and direct-child activity across
  // projects, so nearly every frame touches it — and the roster read is
  // one of the daemon's most expensive. A burst refreshes it once.
  if (/^(agent|session|task|message)\./.test(type)) {
    scheduleCoalescedInvalidation(queryClient, ["agents"], ROSTER_INVALIDATE_MS);
  }

  if (type.startsWith("pool.")) {
    queryClient.invalidateQueries({ queryKey: ["pools"] });
    queryClient.invalidateQueries({ queryKey: ["sessions", "pool"] });
    return;
  }

  if ((type as string) === "dashboard_state.changed.v1") {
    queryClient.invalidateQueries({ queryKey: ["dashboard-state"] });
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
    // A pool's supply is its live sessions; its status row counts them.
    scheduleCoalescedInvalidation(queryClient, ["pools"], ROSTER_INVALIDATE_MS);
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
  // Provider availability (provider-failover D19/D20): a state change, a
  // re-route batch or a half change refetches the availability read the
  // outage banner and the Metrics cards share, and the held-task list.
  if (type.startsWith("provider.") || (type as string) === "notify.provider_state") {
    queryClient.invalidateQueries({ queryKey: ["providers", "availability"] });
    queryClient.invalidateQueries({ queryKey: ["providers", "held-tasks"] });
    return;
  }
  if (type === "proposal.status_changed") {
    const pid = (event as ProposalStatusChangedEvent).proposal_id;
    queryClient.invalidateQueries({ queryKey: ["proposal", pid] });
    return;
  }
  if (type.startsWith("review.")) {
    const reviewId = (event as { review_id?: string }).review_id;
    queryClient.invalidateQueries({ queryKey: ["reviews"] });
    if (reviewId) queryClient.invalidateQueries({ queryKey: ["review", reviewId] });
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
      scheduleCoalescedInvalidation(queryClient, ["agents"], ROSTER_INVALIDATE_MS);
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
      // Delivered to each hook's onTaskMessage callback, not the cache.
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
}

/** Clients whose cache follows the stream, with the number of hooks holding each. */
const cacheClients = new Map<QueryClient, number>();

eventListeners.add((event) => {
  for (const client of cacheClients.keys()) applyEventToCache(client, event);
});

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
    const handleStatus = (status: ConnectionStatus) => {
      if (
        status === "connected"
        && dashboardStateBootstrapGeneration.get(queryClient) !== connectedGeneration
      ) {
        dashboardStateBootstrapGeneration.set(queryClient, connectedGeneration);
        void queryClient.invalidateQueries(
          { queryKey: DASHBOARD_STATE_BOOTSTRAP_KEY, exact: true },
          { cancelRefetch: false },
        );
      }
      onStatusChange?.(status);
    };
    statusListeners.add(handleStatus);
    // Fire current status immediately
    handleStatus(currentStatus);
    return () => { statusListeners.delete(handleStatus); };
  }, [onStatusChange, queryClient]);

  // The cache follows the stream while any hook for this client is mounted.
  useEffect(() => {
    cacheClients.set(queryClient, (cacheClients.get(queryClient) ?? 0) + 1);
    return () => {
      const held = (cacheClients.get(queryClient) ?? 1) - 1;
      if (held > 0) cacheClients.set(queryClient, held);
      else cacheClients.delete(queryClient);
    };
  }, [queryClient]);

  // Per-hook callbacks only; the cache pass above is shared.
  const handleEvent = useCallback(
    (event: NotifyEvent) => {
      onEvent?.(event);
      if (event.event_type === "notify.task_message") onTaskMessage?.(event as TaskMessageEvent);
    },
    [onTaskMessage, onEvent],
  );

  useEffect(() => {
    if (!onEvent && !onTaskMessage) return;
    eventListeners.add(handleEvent);
    return () => { eventListeners.delete(handleEvent); };
  }, [handleEvent, onEvent, onTaskMessage]);
}
