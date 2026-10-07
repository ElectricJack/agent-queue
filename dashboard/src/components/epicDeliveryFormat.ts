import type { ComponentType, SVGProps } from "react";
import {
  ArrowPathIcon,
  CheckBadgeIcon,
  ClockIcon,
  ExclamationTriangleIcon,
  HandRaisedIcon,
  MinusCircleIcon,
  PauseCircleIcon,
  QuestionMarkCircleIcon,
  ShieldCheckIcon,
  WrenchScrewdriverIcon,
} from "@heroicons/react/24/outline";
import type { EpicDeliveryStatus } from "@aq/ts-client";

/** Presentation helpers for an epic's delivery projection (see EpicDelivery.tsx). */
export type EpicDelivery = EpicDeliveryStatus;
export type DeliveryState = EpicDeliveryStatus["state"];

interface StateStyle {
  Icon: ComponentType<SVGProps<SVGSVGElement>>;
  /** Badge colours: decoration only, the text carries the meaning. The badge
   *  lives in the detail views, which stay dark, so these are not graph tokens
   *  (those switch with the graph's theme); the graph's pill takes its tone
   *  from `cardStatus` instead. */
  tone: string;
  /** The card tone (a TaskNode status key) an epic in this state reads as. */
  cardStatus: string | null;
}

const STATE_STYLE: Record<DeliveryState, StateStyle> = {
  implementing: { Icon: WrenchScrewdriverIcon, tone: "bg-white/10 text-gray-200", cardStatus: null },
  queued: { Icon: ClockIcon, tone: "bg-sky-500/15 text-sky-200", cardStatus: "READY" },
  integrating: { Icon: ArrowPathIcon, tone: "bg-indigo-500/20 text-indigo-200", cardStatus: "IN_PROGRESS" },
  verifying: { Icon: ShieldCheckIcon, tone: "bg-indigo-500/20 text-indigo-200", cardStatus: "IN_PROGRESS" },
  blocked: { Icon: ExclamationTriangleIcon, tone: "bg-red-500/15 text-red-200", cardStatus: "BLOCKED" },
  awaiting_approval: { Icon: HandRaisedIcon, tone: "bg-purple-500/15 text-purple-200", cardStatus: "WAITING_INPUT" },
  paused: { Icon: PauseCircleIcon, tone: "bg-amber-500/15 text-amber-200", cardStatus: "PAUSED" },
  delivered: { Icon: CheckBadgeIcon, tone: "bg-emerald-500/15 text-emerald-200", cardStatus: "COMPLETED" },
  unknown: { Icon: QuestionMarkCircleIcon, tone: "bg-gray-500/15 text-gray-300", cardStatus: "PENDING" },
  not_tracked: { Icon: MinusCircleIcon, tone: "bg-gray-500/10 text-gray-400", cardStatus: null },
};

export const EVIDENCE_TEXT: Record<string, string> = {
  stale: "Evidence is stale",
  unavailable: "Evidence unavailable",
};

export function styleFor(state: string): StateStyle {
  return STATE_STYLE[state as DeliveryState] ?? STATE_STYLE.unknown;
}

/** The TaskNode status tone an epic card takes; `fallback` keeps the stored one. */
export function deliveryCardStatus(delivery: EpicDelivery | null | undefined, fallback: string): string {
  if (!delivery) return fallback;
  return styleFor(delivery.state).cardStatus ?? fallback;
}

/** Whole units, largest first: 45s, 12m, 3h, 2d. */
export function formatAge(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function nowSeconds(now?: number): number {
  return now ?? Date.now() / 1000;
}

/** The headline a compact surface shows: implementation keeps its short word. */
export function deliveryHeadline(delivery: EpicDelivery): string {
  return delivery.state === "implementing" ? delivery.display_status : delivery.label;
}

/** One sentence for a tooltip: label, reason, who acts, and how long ago. */
export function describeDelivery(delivery: EpicDelivery, now?: number): string {
  const parts = [delivery.label];
  if (delivery.reason) parts.push(delivery.reason);
  if (delivery.responsible?.label) parts.push(`Responsible: ${delivery.responsible.label}`);
  if (delivery.since != null) parts.push(`Last progress ${formatAge(nowSeconds(now) - delivery.since)} ago`);
  const evidence = EVIDENCE_TEXT[delivery.evidence ?? "current"];
  if (evidence) parts.push(evidence);
  return parts.join(". ");
}

