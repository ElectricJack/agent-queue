import { useEffect, useRef, useState } from "react";

import type { Anchor } from "./anchoring";

export function CommentPopover({
  anchor,
  onSubmit,
  onClose,
}: {
  anchor: Anchor;
  onSubmit: (body: string) => Promise<void> | void;
  onClose: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [body, setBody] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [onClose]);

  const submit = async () => {
    if (!body.trim() || submitting) return;
    setSubmitting(true);
    try {
      await onSubmit(body.trim());
      onClose();
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div ref={rootRef} className="w-72 rounded-lg border border-gray-700 bg-gray-900 p-3 shadow-2xl">
      <p className="mb-2 text-xs text-gray-400">
        {anchor.quote ? `Commenting on “${anchor.quote}”` : `Commenting on ${anchor.heading_path.join(" › ") || "this section"}`}
      </p>
      <label className="sr-only" htmlFor="review-comment-body">Comment</label>
      <textarea
        id="review-comment-body"
        autoFocus
        rows={4}
        value={body}
        onChange={(event) => setBody(event.target.value)}
        className="w-full resize-y rounded border border-gray-700 bg-gray-950 p-2 text-sm text-gray-100 outline-none focus:border-indigo-500"
        placeholder="Write a comment…"
      />
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
    </div>
  );
}
