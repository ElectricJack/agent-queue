import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useEventStreamStatus } from "../ws/EventStreamProvider";

function useOnline(): boolean {
  const [online, setOnline] = useState(() => (typeof navigator === "undefined" ? true : navigator.onLine));
  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);
  return online;
}

/**
 * Says so when the screen may be stale (mobile dashboard §5): the browser is
 * offline, or the live event stream dropped after it had connected. Data stays
 * on screen; Retry refetches the active queries. Nothing is queued.
 */
export default function ConnectionBanner() {
  const status = useEventStreamStatus();
  const online = useOnline();
  const queryClient = useQueryClient();
  const connectedOnce = useRef(false);
  const [lostAt, setLostAt] = useState<number | null>(null);
  useEffect(() => {
    if (status === "connected") {
      connectedOnce.current = true;
      setLostAt(null);
    } else if (connectedOnce.current) {
      setLostAt((at) => at ?? Date.now());
    }
  }, [status]);
  useEffect(() => {
    if (!online) setLostAt((at) => at ?? Date.now());
  }, [online]);
  if (online && lostAt === null) return null;
  return (
    <div className="shrink-0 border-b border-amber-900/60 bg-amber-950/40 px-safe">
      <div role="status" className="flex items-center gap-3 px-3 py-1 text-xs text-amber-100">
        <span className="min-w-0 flex-1">
          {online ? "Live updates paused" : "Offline"} — showing data from{" "}
          {new Date(lostAt ?? Date.now()).toLocaleTimeString()}.
        </span>
        <button
          type="button"
          data-primary-control
          onClick={() => void queryClient.refetchQueries({ type: "active" })}
          className="inline-flex items-center justify-center rounded border border-amber-700 px-3 text-amber-100"
        >
          Retry
        </button>
      </div>
    </div>
  );
}
