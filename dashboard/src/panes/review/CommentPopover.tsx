import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { Anchor } from "./anchoring";
import { useCommentPosition, type CommentReference } from "./useCommentPosition";

export function CommentPopover({
  anchor,
  reference,
  returnFocus,
  onSubmit,
  onClose,
}: {
  anchor: Anchor;
  reference: CommentReference;
  returnFocus: HTMLElement;
  onSubmit: (body: string) => Promise<void> | void;
  onClose: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [body, setBody] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const style = useCommentPosition(reference, rootRef);

  useLayoutEffect(() => {
    // The first measurement reveals the portal; browsers cannot focus a hidden textarea.
    if (style.visibility === "visible") inputRef.current?.focus({ preventScroll: true });
  }, [style.visibility]);

  useEffect(() => () => {
    if (returnFocus.isConnected) returnFocus.focus({ preventScroll: true });
  }, [returnFocus]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
      if (event.key === "Tab" && rootRef.current) {
        const controls = Array.from(rootRef.current.querySelectorAll<HTMLElement>("textarea, button:not(:disabled)"));
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus({ preventScroll: true });
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus({ preventScroll: true });
        }
      }
    };
    const onPointerDown = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [onClose]);

  const submit = async () => {
    if (!body.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onSubmit(body.trim());
      onClose();
    } catch (error) {
      setError(error instanceof Error ? error.message : "Could not submit this comment. Try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return createPortal(
    <div ref={rootRef} role="dialog" aria-label="Add review comment" aria-describedby="review-comment-context"
      style={style} className="z-50 w-72 overflow-y-auto rounded-lg border border-gray-700 bg-gray-900 p-3 shadow-2xl">
      <p id="review-comment-context" className="mb-2 break-words text-xs text-gray-400">
        {anchor.quote ? `Commenting on “${anchor.quote}”` : `Commenting on ${anchor.heading_path.join(" › ") || "this section"}`}
      </p>
      <label className="sr-only" htmlFor="review-comment-body">Comment</label>
      <textarea
        id="review-comment-body"
        ref={inputRef}
        rows={4}
        value={body}
        onChange={(event) => setBody(event.target.value)}
        className="w-full resize-y rounded border border-gray-700 bg-gray-950 p-2 text-sm text-gray-100 outline-none focus:border-indigo-500"
        placeholder="Write a comment…"
      />
      {error && <p role="alert" className="mt-2 text-xs text-red-300">{error}</p>}
      <div className="mt-2 flex justify-end gap-2">
        <button type="button" onClick={onClose} className="rounded px-2 py-1 text-xs text-gray-400 hover:bg-gray-800">Cancel</button>
        <button
          type="button"
          onClick={() => void submit()}
          disabled={!body.trim() || submitting}
          className="rounded bg-indigo-600 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "Submitting…" : "Submit"}
        </button>
      </div>
    </div>,
    document.body,
  );
}
