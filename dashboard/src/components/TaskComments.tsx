import { useId, useState, type FormEvent } from "react";
import { useIsMutating } from "@tanstack/react-query";
import {
  COMMENT_MAX_LENGTH, COMMENT_PAGE_SIZE, commentMutationKey,
  useAddTaskComment, useTaskCommentDraft, useTaskComments,
} from "../api/taskComments";

export default function TaskComments({ taskId }: { taskId: string }) {
  // Reset pagination and mutation errors immediately when switching tasks.
  return <TaskCommentsForTask key={taskId} taskId={taskId} />;
}

function TaskCommentsForTask({ taskId }: { taskId: string }) {
  const headingId = useId();
  const composerId = useId();
  const [offset, setOffset] = useState(0);
  const history = useTaskComments(taskId, offset);
  const [draft, setDraft] = useTaskCommentDraft(taskId);
  const addComment = useAddTaskComment(taskId);
  const isSubmitting = useIsMutating({ mutationKey: commentMutationKey(taskId) }) > 0;
  const page = history.data;
  const comments = page?.comments ?? [];
  const tooLong = Array.from(draft).length > COMMENT_MAX_LENGTH;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim() || tooLong || isSubmitting) return;
    addComment.mutate(draft, { onSuccess: () => setOffset(0) });
  }

  return (
    <section aria-labelledby={headingId} className="space-y-3">
      <h2 id={headingId} className="text-sm font-semibold uppercase text-gray-500">Comments</h2>
      <p className="text-xs text-gray-400">Findings and updates, newest first. Comments do not change task requirements or approvals.</p>
      {history.isPending && <p role="status" className="text-sm text-gray-400">Loading comments…</p>}
      {history.isError && (
        <div role="alert" className="rounded-lg border border-red-800 bg-red-950/30 p-3 text-sm text-red-300">
          Could not load comments. {history.error.message}
          <button type="button" onClick={() => history.refetch()} className="ml-2 underline">Retry comments</button>
        </div>
      )}
      {page && (
        <>
          {comments.length === 0 ? (
            <p className="text-sm text-gray-400">{page.total === 0 ? "No comments yet." : "No comments on this page."}</p>
          ) : (
            <ol className="space-y-2" aria-label="Comment history">
              {comments.map((comment) => (
                <li key={comment.id} className="rounded-lg border border-gray-800 bg-gray-900 p-3">
                  <div className="mb-2 flex flex-wrap justify-between gap-x-3 gap-y-1 text-xs text-gray-400">
                    <span className="break-all">{comment.author_kind} · {comment.author_id}</span>
                    <time dateTime={new Date(comment.created_at * 1000).toISOString()}>
                      {new Date(comment.created_at * 1000).toLocaleString()}
                    </time>
                  </div>
                  <p className="whitespace-pre-wrap break-words text-sm text-gray-200">{comment.body}</p>
                </li>
              ))}
            </ol>
          )}
        </>
      )}
      {(offset > 0 || (page?.total ?? 0) > COMMENT_PAGE_SIZE) && (
        <nav aria-label="Comment pages" className="flex flex-wrap items-center gap-3 text-xs text-gray-400">
          <button type="button" disabled={offset === 0 || history.isFetching}
            onClick={() => setOffset(Math.max(0, offset - COMMENT_PAGE_SIZE))}
            className="rounded border border-gray-700 px-2 py-1 hover:bg-gray-800 disabled:opacity-40">Newer comments</button>
          {page && <span>{offset + (comments.length > 0 ? 1 : 0)}–{offset + comments.length} of {page.total}</span>}
          <button type="button" disabled={!page || offset + COMMENT_PAGE_SIZE >= page.total || history.isFetching}
            onClick={() => setOffset(offset + COMMENT_PAGE_SIZE)}
            className="rounded border border-gray-700 px-2 py-1 hover:bg-gray-800 disabled:opacity-40">Older comments</button>
        </nav>
      )}
      <form onSubmit={submit} className="space-y-2">
        <label htmlFor={composerId} className="block text-sm font-medium text-gray-300">Add a comment</label>
        <textarea id={composerId} value={draft} onChange={(event) => setDraft(event.target.value)} rows={3}
          placeholder="Share a finding or progress update…"
          className="w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:border-indigo-500 focus:outline-none" />
        {tooLong && <p role="alert" className="text-sm text-red-300">Comments must be {COMMENT_MAX_LENGTH.toLocaleString()} characters or fewer.</p>}
        {addComment.isError && <p role="alert" className="text-sm text-red-300">Could not add comment. {addComment.error.message}</p>}
        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-gray-500">Comments are saved to this task’s history.</p>
          <button type="submit" disabled={!draft.trim() || tooLong || isSubmitting}
            className="shrink-0 rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50">
            {isSubmitting ? "Adding…" : "Add comment"}
          </button>
        </div>
      </form>
    </section>
  );
}
