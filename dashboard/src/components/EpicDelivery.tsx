import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { EpicDeliveryRef } from "@aq/ts-client";
import { ProgressBar } from "../pages/command-center/ProgressBar";
import {
  EVIDENCE_TEXT,
  deliveryHeadline,
  describeDelivery,
  formatAge,
  nowSeconds,
  styleFor,
  type EpicDelivery,
} from "./epicDeliveryFormat";

/**
 * An epic's implementation progress is shown apart from its delivery
 * (docs/superpowers/specs/2026-10-02-epic-delivery-status-design.md). The
 * daemon's read-only projection decides the state; this module only draws it.
 * Every state has its own icon and visible text, so nothing is told by colour
 * alone.
 */

/** Icon plus visible text; `text` overrides the headline (e.g. the short status word). */
export function EpicDeliveryBadge({
  delivery,
  text,
  now,
  className = "",
}: {
  delivery: EpicDelivery;
  text?: string;
  now?: number;
  className?: string;
}) {
  const { Icon, tone } = styleFor(delivery.state);
  const shown = text ?? deliveryHeadline(delivery);
  return (
    <span
      data-delivery-state={delivery.state}
      title={describeDelivery(delivery, now)}
      className={`inline-flex min-w-0 items-center gap-1 rounded px-1 ${tone} ${className}`}
    >
      <Icon aria-hidden className="h-3 w-3 shrink-0" />
      <span className="truncate">{shown}</span>
    </span>
  );
}

const HOLD_NOTE: Record<string, string> = {
  integration: "held by integration",
  backoff: "cooling down",
};

/**
 * A detail header's status for an epic: the delivery word, plus a note when the
 * stored status is a PAUSED nobody chose (pause and resume controls still act
 * on the stored status).
 */
export function EpicStatus({ delivery, status = "" }: { delivery: EpicDelivery; status?: string }) {
  const note = status === "PAUSED" ? HOLD_NOTE[delivery.hold ?? ""] : undefined;
  return (
    <span className="inline-flex items-center gap-1.5">
      <EpicDeliveryBadge
        delivery={delivery}
        text={delivery.display_status}
        className="rounded-full px-2 py-0.5 text-xs font-medium"
      />
      {note && (
        <span className="text-gray-500" title={`Stored task status: ${status}`}>
          {note}
        </span>
      )}
    </span>
  );
}

function RefLink({ item, onOpenTask }: { item: EpicDeliveryRef; onOpenTask?: (taskId: string) => void }) {
  const id = item.id ?? "";
  if (item.kind === "task" && id) {
    if (onOpenTask) {
      return (
        <button type="button" onClick={() => onOpenTask(id)} className="text-indigo-400 hover:underline" title={id}>
          {item.label || id}
        </button>
      );
    }
    return (
      <Link to={`/tasks/${encodeURIComponent(id)}`} className="text-indigo-400 hover:underline" title={id}>
        {item.label || id}
      </Link>
    );
  }
  if (!id) return <span>{item.label}</span>;
  // Operations, batches and gates have no page of their own; the id is what
  // `aq integration status` and `aq gate show` take.
  return (
    <span className="inline-flex flex-wrap items-baseline gap-1">
      <span>{item.label}</span>
      <code className="select-all break-all rounded bg-gray-800 px-1 font-mono text-[11px] text-gray-300">{id}</code>
    </span>
  );
}

function Row({ term, children }: { term: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-gray-500">{term}</dt>
      <dd className="min-w-0 text-gray-200 [overflow-wrap:anywhere]">{children}</dd>
    </>
  );
}

/** The detail-view section: implementation progress, then delivery and its evidence. */
export function EpicDeliveryPanel({
  delivery,
  onOpenTask,
  now,
}: {
  delivery: EpicDelivery;
  onOpenTask?: (taskId: string) => void;
  now?: number;
}) {
  const done = delivery.implementation_completed ?? 0;
  const total = delivery.implementation_total ?? 0;
  const links = delivery.links ?? [];
  const evidence = EVIDENCE_TEXT[delivery.evidence ?? "current"];
  const current = nowSeconds(now);
  return (
    <section aria-label="Implementation and delivery" data-testid="epic-delivery">
      <h3 className="mb-1.5 text-xs font-semibold uppercase text-gray-500">Implementation &amp; delivery</h3>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1.5 text-xs">
        <Row term="Implementation">
          <span className="block">{done}/{total} tasks complete</span>
          <ProgressBar className="mt-1" done={done} total={total} />
        </Row>
        <Row term="Delivery">
          <EpicDeliveryBadge delivery={delivery} text={delivery.label} now={now} className="py-0.5 text-xs" />
        </Row>
        {delivery.reason && <Row term="Reason">{delivery.reason}</Row>}
        {delivery.responsible && (
          <Row term="Responsible">
            {delivery.responsible.kind === "task"
              ? <RefLink item={delivery.responsible} onOpenTask={onOpenTask} />
              : <span>{delivery.responsible.label}</span>}
          </Row>
        )}
        {delivery.since != null && (
          <Row term="Last progress">
            <time dateTime={new Date(delivery.since * 1000).toISOString()} title={new Date(delivery.since * 1000).toLocaleString()}>
              {formatAge(current - delivery.since)} ago
            </time>
          </Row>
        )}
        {evidence && <Row term="Evidence">{evidence}</Row>}
        {delivery.remedy && (
          <Row term="Next step">
            <code className="select-all break-all rounded bg-gray-800 px-1 font-mono text-[11px] text-gray-300">{delivery.remedy}</code>
            <span className="ml-1 text-gray-500">(dry run; it repeats every check)</span>
          </Row>
        )}
        {links.length > 0 && (
          <Row term="Details">
            <ul className="space-y-0.5">
              {links.map((item) => (
                <li key={`${item.kind}:${item.id ?? item.label}`}>
                  <RefLink item={item} onOpenTask={onOpenTask} />
                </li>
              ))}
            </ul>
          </Row>
        )}
      </dl>
    </section>
  );
}
