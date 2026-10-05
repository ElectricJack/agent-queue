import { Link } from "react-router-dom";

import { useEscalations } from "../../api/messaging";
import { useReviews } from "../../api/reviews";
import type { ReviewRecord } from "@aq/ts-client";
import { FocusError } from "./FocusNotice";
import { useFocusChrome } from "./focusChrome";
import { focusEscalationHref, focusReviewHref } from "./routes";

/** `OPEN_ESCALATION_STATES` in `escalation_queries.py`. */
const OPEN_STATES = new Set(["needs_human", "reply_received", "resolving"]);
/** The deciders ReviewsInbox treats as waiting on the human (reviews/service.py). */
const HUMAN_DECIDERS = new Set(["user", "user_or_supervisor"]);

const SECTION = "rounded-lg border border-gray-800 p-3";

function age(createdAt: number | null | undefined): string {
  if (!createdAt) return "—";
  return new Date(createdAt * 1000).toLocaleString();
}

function isWaitingOnUser(review: ReviewRecord): boolean {
  return review.state === "in_review" && HUMAN_DECIDERS.has(review.decider ?? "");
}

/**
 * `/focus/inbox` — the one page that answers "what needs me", across every
 * project (spec §6.1). Two lists and nothing else: the open escalations that
 * are asking for a decision, and the document reviews whose decider is the
 * human. Both open the focus page for the item, so a phone never leaves focus.
 */
export default function FocusInbox() {
  useFocusChrome({ title: "Needs you" });
  const escalations = useEscalations();
  const reviews = useReviews({ state: "in_review" });

  if (escalations.isError) {
    return (
      <FocusError
        title="Needs you"
        message={escalations.error instanceof Error ? escalations.error.message : "Escalations could not be read."}
        onRetry={() => void escalations.refetch()}
      />
    );
  }

  const open = (escalations.data?.escalations ?? []).filter((item) => OPEN_STATES.has(item.state));
  const waiting = (reviews.data?.reviews ?? []).filter(isWaitingOnUser);

  return (
    <div className="space-y-4 p-3">
      <section className={SECTION}>
        <h2 className="text-sm font-semibold text-gray-200">
          Needs a decision · {open.length}
        </h2>
        {escalations.isLoading && <p className="pt-2 text-sm text-gray-500">Loading…</p>}
        {!escalations.isLoading && open.length === 0 && (
          <p className="pt-2 text-sm text-gray-500">No open escalations.</p>
        )}
        <ul className="divide-y divide-gray-800">
          {open.map((item) => (
            <li key={item.id}>
              <Link
                to={focusEscalationHref(item.id)}
                className="block py-2 text-sm hover:bg-gray-900"
              >
                <span className="block font-medium text-gray-100">{item.decision_requested || item.summary}</span>
                <span className="block text-xs text-gray-500">
                  {item.project_id} · {item.severity} · opened {age(item.created_at)}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </section>

      <section className={SECTION}>
        <h2 className="text-sm font-semibold text-gray-200">
          Reviews waiting on you · {waiting.length}
        </h2>
        {reviews.isLoading && <p className="pt-2 text-sm text-gray-500">Loading…</p>}
        {!reviews.isLoading && reviews.isError && (
          <p role="alert" className="pt-2 text-sm text-red-300">
            {reviews.error instanceof Error ? reviews.error.message : "Reviews could not be read."}
          </p>
        )}
        {!reviews.isLoading && !reviews.isError && waiting.length === 0 && (
          <p className="pt-2 text-sm text-gray-500">No reviews are waiting on you.</p>
        )}
        <ul className="divide-y divide-gray-800">
          {waiting.map((review) => (
            <li key={review.id}>
              <Link to={focusReviewHref(review.id)} className="block py-2 text-sm hover:bg-gray-900">
                <span className="block font-medium text-gray-100">{review.title}</span>
                <span className="block text-xs text-gray-500">
                  {review.kind} · revision {review.current_revision} · {review.project_id}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}