export type ReviewComment = {
  id?: string;
  body?: string;
  quote: string | null;
  heading_path?: string[];
  revision: number;
  resolved?: boolean;
  resolved_at?: unknown;
};

function CommentCard({ comment }: { comment: ReviewComment }) {
  const resolved = comment.resolved === true || comment.resolved_at != null;
  return (
    <article className="rounded border border-gray-800 bg-gray-950/70 p-3 text-xs text-gray-300">
      <div className="mb-1 flex items-center gap-2 text-gray-500">
        <span>rev {comment.revision}</span>
        {resolved && <span className="rounded bg-gray-800 px-1.5 py-0.5">resolved</span>}
      </div>
      {comment.quote && <blockquote className="mb-2 border-l-2 border-indigo-700 pl-2 text-gray-400">{comment.quote}</blockquote>}
      {comment.heading_path && comment.heading_path.length > 0 && !comment.quote && (
        <p className="mb-2 text-gray-500">{comment.heading_path.join(" › ")}</p>
      )}
      <p className="whitespace-pre-wrap">{comment.body ?? ""}</p>
    </article>
  );
}

/** Margin companion for comments which still match the displayed revision. */
export function CommentMargin({ anchored, earlier }: { anchored: ReviewComment[]; earlier: ReviewComment[] }) {
  if (anchored.length === 0 && earlier.length === 0) {
    return <aside className="hidden w-[260px] shrink-0 border-l border-gray-800 p-3 min-[720px]:block"><p className="text-xs text-gray-600">No comments yet.</p></aside>;
  }
  return (
    <aside className="hidden w-[260px] shrink-0 space-y-3 overflow-y-auto border-l border-gray-800 p-3 min-[720px]:block" aria-label="Review comments">
      {anchored.map((comment, index) => <CommentCard key={comment.id ?? `comment-${index}`} comment={comment} />)}
      {earlier.length > 0 && (
        <section className="space-y-2 border-t border-gray-800 pt-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-gray-500">Earlier comments</h2>
          {earlier.map((comment, index) => <CommentCard key={comment.id ?? `earlier-${index}`} comment={comment} />)}
        </section>
      )}
    </aside>
  );
}
