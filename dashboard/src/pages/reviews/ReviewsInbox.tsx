import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useReviews } from "../../api/reviews";
import PullRequestsSection from "./PullRequestsSection";
import WithdrawReviewModal from "./WithdrawReviewModal";

const STATES = ["in_review", "changes_requested", "rejected", "approved", "withdrawn"];
/** States a review can still be withdrawn from (`OPEN_STATES` in src/reviews/service.py). */
const OPEN_STATES = new Set(["in_review", "changes_requested", "rejected"]);
const KINDS = ["spec", "plan", "other"];

type InboxReview = {
  id: string;
  title: string;
  kind: string;
  project_id: string;
  author_task_id?: string | null;
  state: string;
  decider?: string;
  current_revision: number;
  created_at?: unknown;
  updated_at?: unknown;
  open_comment_count?: unknown;
};

function epoch(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function waitingTime(review: InboxReview): string {
  const time = epoch(review.updated_at) ?? epoch(review.created_at);
  return time == null ? "—" : new Date(time * 1000).toLocaleString();
}

function openComments(review: InboxReview): number {
  return typeof review.open_comment_count === "number" ? review.open_comment_count : 0;
}

function isWaitingForUser(review: InboxReview): boolean {
  return review.state === "in_review"
    && (review.decider === "user" || review.decider === "user_or_supervisor");
}

type ClosedRow = { review: InboxReview; index: number };
type ClosedRows = { filters: string; rows: Map<string, ClosedRow> };

/**
 * Keep the rows closed on this page where they were, now withdrawn, even after
 * the refetch drops them from an open-state list. Changing a filter clears them.
 */
function withClosedRows(rows: InboxReview[], closed: Map<string, ClosedRow>): InboxReview[] {
  const merged = rows.map((row) => (
    OPEN_STATES.has(row.state) ? closed.get(row.id)?.review ?? row : row
  ));
  const present = new Set(rows.map((row) => row.id));
  [...closed.values()]
    .filter(({ review }) => !present.has(review.id))
    .sort((a, b) => a.index - b.index)
    .forEach(({ review, index }) => merged.splice(Math.min(index, merged.length), 0, review));
  return merged;
}

const STATE_TONES: Record<string, string> = {
  approved: "bg-emerald-500/15 text-emerald-200",
  withdrawn: "bg-gray-700/60 text-gray-300",
};

function StateBadge({ state }: { state: string }) {
  const tone = STATE_TONES[state] ?? "bg-indigo-500/15 text-indigo-200";
  return <span className={`rounded px-1.5 py-0.5 text-xs ${tone}`}>{state.replace(/_/g, " ")}</span>;
}

/** URL-addressable inbox for local-operator document decisions. */
export default function ReviewsInbox() {
  const [params, setParams] = useSearchParams();
  const stateParam = params.get("state");
  const kind = params.get("kind") ?? "";
  const projectId = params.get("project") ?? "";
  const waiting = stateParam == null;
  const state = stateParam ?? "in_review";
  const reviews = useReviews({
    state,
    ...(kind ? { kind } : {}),
    ...(projectId ? { projectId } : {}),
  });
  const filters = `${stateParam ?? ""}|${kind}|${projectId}`;
  const [closed, setClosed] = useState<ClosedRows>({ filters, rows: new Map() });
  const [closing, setClosing] = useState<InboxReview | null>(null);

  const update = (key: "state" | "kind" | "project", value: string) => {
    const next = new URLSearchParams(params);
    if (!value || (key === "state" && value === "waiting")) next.delete(key);
    else next.set(key, value);
    setParams(next);
  };

  const visible = withClosedRows(
    ((reviews.data?.reviews ?? []) as InboxReview[]).filter(
      (review) => !waiting || isWaitingForUser(review),
    ),
    closed.filters === filters ? closed.rows : new Map<string, ClosedRow>(),
  );

  const markWithdrawn = (review: InboxReview) => {
    const index = Math.max(0, visible.findIndex((row) => row.id === review.id));
    setClosed((current) => ({
      filters,
      rows: new Map<string, ClosedRow>(current.filters === filters ? current.rows : [])
        .set(review.id, { review: { ...review, state: "withdrawn" }, index }),
    }));
    setClosing(null);
  };

  return (
    <div className="dashboard-scrollbar h-full space-y-5 overflow-y-auto p-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-gray-100">Reviews</h1>
          <p className="text-xs text-gray-500">Pending pull requests and document reviews.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <label className="text-xs text-gray-400">
            State
            <select
              aria-label="Review state"
              value={waiting ? "waiting" : state}
              onChange={(event) => update("state", event.target.value)}
              className="ml-1 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-gray-200"
            >
              <option value="waiting">Waiting for you</option>
              {STATES.map((value) => <option key={value} value={value}>{value.replace(/_/g, " ")}</option>)}
            </select>
          </label>
          <label className="text-xs text-gray-400">
            Kind
            <select
              aria-label="Review kind"
              value={kind}
              onChange={(event) => update("kind", event.target.value)}
              className="ml-1 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-gray-200"
            >
              <option value="">All kinds</option>
              {KINDS.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </label>
          <label className="text-xs text-gray-400">
            Project
            <input
              aria-label="Review project"
              value={projectId}
              onChange={(event) => update("project", event.target.value)}
              className="ml-1 w-32 rounded border border-gray-700 bg-gray-900 px-2 py-1 text-gray-200"
              placeholder="All projects"
            />
          </label>
        </div>
      </header>

      <PullRequestsSection />

      <h2 className="text-base font-semibold text-gray-100">Document reviews</h2>

      {reviews.isLoading && <p className="text-sm text-gray-500">Loading reviews…</p>}
      {reviews.error && <p role="alert" className="text-sm text-red-300">Could not load reviews.</p>}
      {!reviews.isLoading && visible.length === 0 && (
        <p className="rounded-lg border border-dashed border-gray-800 p-5 text-sm text-gray-500">
          No reviews match these filters.
        </p>
      )}

      {visible.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-800">
          <table className="w-full min-w-[880px] text-left text-sm">
            <thead className="bg-gray-900 text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Title</th><th className="px-3 py-2">State</th>
                <th className="px-3 py-2">Kind</th>
                <th className="px-3 py-2">Project</th><th className="px-3 py-2">Author task</th>
                <th className="px-3 py-2">Revision</th><th className="px-3 py-2">Waiting</th>
                <th className="px-3 py-2">Comments</th>
                <th className="px-3 py-2"><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {visible.map((review) => (
                <tr
                  key={review.id}
                  className={`hover:bg-gray-900/60 ${review.state === "withdrawn" ? "opacity-60" : ""}`}
                >
                  <td className="px-3 py-2.5">
                    <Link className="font-medium text-indigo-300 hover:underline" to={`/reviews/${encodeURIComponent(review.id)}`}>
                      {review.title}
                    </Link>
                    {review.decider === "user_or_supervisor" && (
                      <span className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 text-xs text-amber-200">delegated</span>
                    )}
                  </td>
                  <td className="px-3 py-2.5"><StateBadge state={review.state} /></td>
                  <td className="px-3 py-2.5 text-gray-300">{review.kind}</td>
                  <td className="px-3 py-2.5 font-mono text-xs text-gray-400">{review.project_id}</td>
                  <td className="px-3 py-2.5 font-mono text-xs text-gray-400">{review.author_task_id ?? "—"}</td>
                  <td className="px-3 py-2.5 text-gray-300">rev {review.current_revision}</td>
                  <td className="px-3 py-2.5 text-xs text-gray-400">{waitingTime(review)}</td>
                  <td className="px-3 py-2.5 text-gray-300">{openComments(review)}</td>
                  <td className="px-3 py-2.5 text-right">
                    {OPEN_STATES.has(review.state) && (
                      <button
                        type="button"
                        aria-label={`Close review ${review.title}`}
                        onClick={() => setClosing(review)}
                        className="rounded border border-gray-700 px-2 py-1 text-xs text-gray-300 hover:border-red-500/60 hover:text-red-200"
                      >
                        Close
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {closing && (
        <WithdrawReviewModal
          review={closing}
          onClose={() => setClosing(null)}
          onWithdrawn={() => markWithdrawn(closing)}
        />
      )}
    </div>
  );
}
