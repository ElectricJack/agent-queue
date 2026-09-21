import { Link, useSearchParams } from "react-router-dom";

import { useReviews } from "../../api/reviews";

const STATES = ["in_review", "changes_requested", "approved", "withdrawn"];
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

  const update = (key: "state" | "kind" | "project", value: string) => {
    const next = new URLSearchParams(params);
    if (!value || (key === "state" && value === "waiting")) next.delete(key);
    else next.set(key, value);
    setParams(next);
  };

  const visible = ((reviews.data?.reviews ?? []) as InboxReview[]).filter(
    (review) => !waiting || isWaitingForUser(review),
  );

  return (
    <div className="dashboard-scrollbar h-full space-y-5 overflow-y-auto p-5">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-gray-100">Reviews</h1>
          <p className="text-xs text-gray-500">Documents awaiting review and their decision history.</p>
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

      {reviews.isLoading && <p className="text-sm text-gray-500">Loading reviews…</p>}
      {reviews.error && <p role="alert" className="text-sm text-red-300">Could not load reviews.</p>}
      {!reviews.isLoading && visible.length === 0 && (
        <p className="rounded-lg border border-dashed border-gray-800 p-5 text-sm text-gray-500">
          No reviews match these filters.
        </p>
      )}

      {visible.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-gray-800">
          <table className="w-full min-w-[760px] text-left text-sm">
            <thead className="bg-gray-900 text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-3 py-2">Title</th><th className="px-3 py-2">Kind</th>
                <th className="px-3 py-2">Project</th><th className="px-3 py-2">Author task</th>
                <th className="px-3 py-2">Revision</th><th className="px-3 py-2">Waiting</th>
                <th className="px-3 py-2">Comments</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-800">
              {visible.map((review) => (
                <tr key={review.id} className="hover:bg-gray-900/60">
                  <td className="px-3 py-2.5">
                    <Link className="font-medium text-indigo-300 hover:underline" to={`/reviews/${encodeURIComponent(review.id)}`}>
                      {review.title}
                    </Link>
                    {review.decider === "user_or_supervisor" && (
                      <span className="ml-2 rounded bg-amber-500/15 px-1.5 py-0.5 text-xs text-amber-200">delegated</span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-gray-300">{review.kind}</td>
                  <td className="px-3 py-2.5 font-mono text-xs text-gray-400">{review.project_id}</td>
                  <td className="px-3 py-2.5 font-mono text-xs text-gray-400">{review.author_task_id ?? "—"}</td>
                  <td className="px-3 py-2.5 text-gray-300">rev {review.current_revision}</td>
                  <td className="px-3 py-2.5 text-xs text-gray-400">{waitingTime(review)}</td>
                  <td className="px-3 py-2.5 text-gray-300">{openComments(review)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
