import type { EpicDeliveryStatus } from "@aq/ts-client";

/**
 * Representative daemon answers for an epic's delivery projection, one per
 * scenario the design spec names
 * (docs/superpowers/specs/2026-10-02-epic-delivery-status-design.md). The
 * labels and reasons are the ones `src/integration/epic_delivery.py` writes.
 */
export const NOW = 1_790_980_000;

const base: Omit<EpicDeliveryStatus, "state" | "label" | "display_status"> = {
  hold: "integration",
  reason: null,
  remedy: null,
  responsible: null,
  since: null,
  links: [],
  evidence: "current",
  implementation_completed: 5,
  implementation_total: 5,
};

export const EPIC_DELIVERY = {
  implementing: {
    ...base,
    state: "implementing",
    label: "Implementation in progress",
    display_status: "In progress",
    reason: "3 of 5 tasks still open",
    since: NOW - 60,
    implementation_completed: 2,
  },
  activeIntegration: {
    ...base,
    state: "integrating",
    label: "Integrating",
    display_status: "Integrating",
    reason: "Repair stage 0 writer is working",
    responsible: { kind: "session", id: "sess-1", label: "standard-high-claude sess-1" },
    since: NOW - 90,
    links: [
      { kind: "task", id: "repair-op-0", label: "Integration task repair-op-0" },
      { kind: "operation", id: "op-1", label: "Operation op-1" },
    ],
  },
  queuedVerifier: {
    ...base,
    state: "queued",
    label: "Verification queued - waiting for a worker",
    display_status: "Queued",
    reason: "verify-op-1 is READY and claimable",
    responsible: { kind: "system", id: null, label: "Worker pool" },
    since: NOW - 300,
    links: [{ kind: "task", id: "verify-op-1", label: "Verification task verify-op-1" }],
  },
  strandedReservation: {
    ...base,
    state: "blocked",
    label: "Verification blocked - branch handoff required",
    display_status: "Delivery blocked",
    reason:
      "Repair stage 13 still holds the branch reservation (reserved); the verification task cannot claim the branch until it is handed off",
    responsible: { kind: "task", id: "repair-op-13", label: "Repair stage 13" },
    since: NOW - 3 * 3600,
    links: [
      { kind: "task", id: "verify-op-1", label: "Verification task verify-op-1" },
      { kind: "task", id: "repair-op-13", label: "Reservation holder repair-op-13" },
      { kind: "operation", id: "op-1", label: "Operation op-1" },
    ],
    implementation_completed: 9,
    implementation_total: 9,
  },
  missingReceipt: {
    ...base,
    state: "blocked",
    label: "Integration blocked - final fix not collected",
    display_status: "Delivery blocked",
    reason:
      "calm-grove-25.5 completed after the aggregate was frozen for verification; no delivery receipt collects it",
    responsible: { kind: "operator", id: null, label: "Operator" },
    remedy: "aq integration reopen-collection calm-grove-25",
    since: NOW - 2 * 3600,
    links: [
      { kind: "task", id: "calm-grove-25.5", label: "Fix K04 knowledge_export API scope" },
      { kind: "operation", id: "op-2", label: "Operation op-2" },
    ],
  },
  manualPause: {
    ...base,
    state: "paused",
    label: "Paused by operator",
    display_status: "Paused",
    hold: "operator",
    reason: "Paused manually; Resume releases it",
    responsible: { kind: "operator", id: null, label: "Operator" },
    since: NOW - 600,
    implementation_completed: 2,
  },
  approvalHold: {
    ...base,
    state: "awaiting_approval",
    label: "Awaiting approval",
    display_status: "Awaiting approval",
    hold: null,
    reason: "Approve the release plan",
    responsible: { kind: "operator", id: null, label: "Operator" },
    since: NOW - 1800,
    links: [{ kind: "gate", id: "gate-1", label: "Gate gate-1" }],
  },
  delivered: {
    ...base,
    state: "delivered",
    label: "Delivered",
    display_status: "Delivered",
    hold: null,
    reason: "Delivered to main by train batch batch-1",
    since: NOW - 86400,
    links: [{ kind: "batch", id: "batch-1", label: "Batch batch-1" }],
  },
  staleEvidence: {
    ...base,
    state: "unknown",
    label: "Verification status stale",
    display_status: "Delivery unknown",
    reason: "Session sess-2 has shown no activity for 25m (lease 8m)",
    responsible: { kind: "session", id: "sess-2", label: "standard-high-claude sess-2" },
    since: NOW - 1500,
    evidence: "stale",
  },
  unavailable: {
    ...base,
    state: "unknown",
    label: "Delivery evidence unavailable",
    display_status: "Delivery unknown",
    reason: "Verifier task verify-op-3 is no longer in the queue",
    evidence: "unavailable",
  },
} satisfies Record<string, EpicDeliveryStatus>;
