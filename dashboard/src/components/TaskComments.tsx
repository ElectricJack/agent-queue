import { useId, useState, type FormEvent } from "react";
import { useIsMutating } from "@tanstack/react-query";
import {
  COMMENT_MAX_LENGTH, COMMENT_PAGE_SIZE, commentMutationKey,
  useAddTaskComment, useDeleteTaskComment, useEditTaskComment, useTaskCommentDraft, useTaskComments,
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
  const editComment = useEditTaskComment(taskId);
  const deleteComment = useDeleteTaskComment(taskId);
  // One comment at a time is open for editing or awaiting delete confirmation.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const editTooLong = Array.from(editDraft).length > COMMENT_MAX_LENGTH;
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
                    <span className="flex items-center gap-2">
                      <time dateTime={new Date(comment.created_at * 1000).toISOString()}>
                        {new Date(comment.created_at * 1000).toLocaleString()}
                      </time>
                      {editingId !== comment.id && confirmDeleteId !== comment.id && (
                        <>
                          <button type="button" aria-label="Edit comment" disabled={isSubmitting}
                            onClick={() => { setConfirmDeleteId(null); setEditingId(comment.id); setEditDraft(comment.body); editComment.reset(); }}
                            className="text-indigo-400 hover:text-indigo-300 disabled:opacity-50">Edit</button>
                          <button type="button" aria-label="Delete comment" disabled={isSubmitting}
                            onClick={() => { setEditingId(null); setConfirmDeleteId(comment.id); deleteComment.reset(); }}
                            className="text-gray-400 hover:text-red-300 disabled:opacity-50">Delete</button>
                        </>
                      )}
                    </span>
                  </div>
                  {editingId === comment.id ? (
                    <form className="space-y-2" onSubmit={(event) => {
                      event.preventDefault();
                      if (!editDraft.trim() || editTooLong || isSubmitting) return;
                      editComment.mutate({ commentId: comment.id, body: editDraft }, { onSuccess: () => setEditingId(null) });
                    }}>
                      <label htmlFor={`${composerId}-edit`} className="sr-only">Edit comment text</label>
                      <textarea id={`${composerId}-edit`} value={editDraft} onChange={(event) => setEditDraft(event.target.value)} rows={3}
                        disabled={isSubmitting}
                        className="w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none" />
                      {editTooLong && <p role="alert" className="text-sm text-red-300">Comments must be {COMMENT_MAX_LENGTH.toLocaleString()} characters or fewer.</p>}
                      {editComment.isError && <p role="alert" className="text-sm text-red-300">Could not save comment. {editComment.error.message}</p>}
                      <div className="flex justify-end gap-2">
                        <button type="button" aria-label="Cancel comment edit" disabled={isSubmitting}
                          onClick={() => { setEditingId(null); editComment.reset(); }}
                          className="rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-300 disabled:opacity-50">Cancel</button>
                        <button type="submit" disabled={!editDraft.trim() || editTooLong || isSubmitting || editDraft === comment.body}
                          className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white disabled:opacity-50">
                          {isSubmitting ? "Saving comment…" : "Save comment"}
                        </button>
                      </div>
                    </form>
                  ) : (
                    <p className="whitespace-pre-wrap break-words text-sm text-gray-200">{comment.body}</p>
                  )}
                  {confirmDeleteId === comment.id && (
                    <div className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded border border-red-900/60 bg-red-950/30 px-3 py-2 text-sm text-red-200">
                      <span>Delete this comment? This cannot be undone.</span>
                      <span className="flex gap-2">
                        <button type="button" aria-label="Cancel comment delete" disabled={isSubmitting}
                          onClick={() => { setConfirmDeleteId(null); deleteComment.reset(); }}
                          className="rounded border border-gray-700 px-2 py-1 text-xs text-gray-300 disabled:opacity-50">Keep</button>
                        <button type="button" aria-label="Confirm comment delete" disabled={isSubmitting}
                          onClick={() => deleteComment.mutate(comment.id, { onSuccess: () => setConfirmDeleteId(null) })}
                          className="rounded bg-red-700 px-2 py-1 text-xs text-white hover:bg-red-600 disabled:opacity-50">
                          {isSubmitting ? "Deleting…" : "Delete comment"}
                        </button>
                      </span>
                      {deleteComment.isError && <p role="alert" className="w-full text-red-300">Could not delete comment. {deleteComment.error.message}</p>}
                    </div>
                  )}
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
