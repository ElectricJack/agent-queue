import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { XMarkIcon } from "@heroicons/react/24/outline";

import { useRawEventSubscription } from "../ws/useEventStream";
import type { NotifyEvent } from "../ws/types";

type Toast = { id: string; reviewId: string; message: string };
const DISMISS_MS = 8_000;

/** Live review notifications stay available while navigating across the shell. */
export default function ReviewToasts() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const dismiss = useCallback((id: string) => {
    const timer = timers.current.get(id);
    if (timer) clearTimeout(timer);
    timers.current.delete(id);
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  useEffect(() => () => {
    for (const timer of timers.current.values()) clearTimeout(timer);
    timers.current.clear();
  }, []);

  const onEvent = useCallback((event: NotifyEvent) => {
    if (event.event_type !== "review.submitted" && event.event_type !== "review.revised") return;
    const review = event as NotifyEvent & { review_id?: string; title?: string; kind?: string; revision?: number };
    if (!review.review_id || !review.title) return;
    const id = `${event.event_type}:${review.review_id}:${review.revision ?? "current"}`;
    const message = event.event_type === "review.submitted"
      ? `New ${review.kind ?? "document"} for review: ${review.title}`
      : `Revised: ${review.title} (rev ${review.revision ?? "?"})`;
    setToasts((current) => [...current.filter((toast) => toast.id !== id), { id, reviewId: review.review_id!, message }]);
    const oldTimer = timers.current.get(id);
    if (oldTimer) clearTimeout(oldTimer);
    timers.current.set(id, setTimeout(() => dismiss(id), DISMISS_MS));
  }, [dismiss]);

  useRawEventSubscription(onEvent);

  if (toasts.length === 0) return null;
  return (
    <div className="fixed bottom-4 left-1/2 z-50 flex w-[min(30rem,calc(100vw-2rem))] -translate-x-1/2 flex-col gap-2">
      {toasts.map((toast) => (
        <div key={toast.id} role="status" className="flex items-center gap-3 rounded-lg border border-indigo-500/40 bg-gray-900 px-3 py-2 text-sm text-gray-100 shadow-xl">
          <span className="min-w-0 flex-1">{toast.message}</span>
          <Link className="rounded bg-indigo-600 px-2 py-1 text-xs font-medium text-white hover:bg-indigo-500" to={`/reviews/${encodeURIComponent(toast.reviewId)}`}>Open</Link>
          <button type="button" aria-label="Dismiss review notification" onClick={() => dismiss(toast.id)} className="rounded p-1 text-gray-400 hover:bg-gray-800 hover:text-gray-100">
            <XMarkIcon className="h-4 w-4" />
          </button>
        </div>
      ))}
    </div>
  );
}
