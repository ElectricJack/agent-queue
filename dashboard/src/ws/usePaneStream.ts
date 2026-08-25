/**
 * SSE hook wrapping `GET /api/sessions/{session_id}/pane`.
 *
 * Unlike useTranscriptStream, this holds ONE current screen, not a buffer:
 * each frame is a full `capture-pane` snapshot that supersedes the last, so
 * accumulating them would only grow memory to redraw the same terminal.
 *
 * Frame shapes (src/api/pane_stream.py):
 *   {source:"pane", type:"screen",  screen, seq, ts}
 *   {source:"pane", type:"stopped", seq, ts}
 *   {source:"pane", type:"error",   message, seq, ts}
 */
import { useEffect, useRef, useState } from "react";

export type PaneStatus = "connecting" | "open" | "stopped" | "error" | "closed";

export interface PaneState {
  screen: string | null;
  status: PaneStatus;
  error: string | null;
  seq: number;
}

interface Options {
  enabled?: boolean;
}

const INITIAL: PaneState = { screen: null, status: "closed", error: null, seq: 0 };

export function usePaneStream(
  sessionId: string | null | undefined,
  opts: Options = {},
): PaneState {
  const { enabled = true } = opts;
  const [state, setState] = useState<PaneState>(INITIAL);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!enabled || !sessionId) return;

    const base =
      import.meta.env.VITE_API_URL ||
      `${window.location.protocol}//${window.location.host}`;
    const url = `${base}/api/sessions/${encodeURIComponent(sessionId)}/pane`;

    setState({ ...INITIAL, status: "connecting" });
    const es = new EventSource(url);
    esRef.current = es;

    es.onopen = () => setState((p) => ({ ...p, status: "open" }));

    es.onmessage = (msg) => {
      try {
        const f = JSON.parse(msg.data) as {
          type?: string;
          screen?: string;
          message?: string;
          seq?: number;
        };
        setState((prev) => {
          const seq = f.seq ?? prev.seq;
          if (f.type === "stopped") return { ...prev, status: "stopped", seq };
          if (f.type === "error")
            return {
              ...prev,
              status: "error",
              error: f.message ?? "pane stream error",
              seq,
            };
          return {
            screen: f.screen ?? prev.screen,
            status: "open",
            error: null,
            seq,
          };
        });
      } catch {
        // Malformed frame; heartbeats are comments and never land here.
      }
    };

    es.onerror = () =>
      setState((p) => ({
        ...p,
        status: "error",
        error: "stream error (EventSource will retry)",
      }));

    return () => {
      es.close();
      esRef.current = null;
      setState((p) => ({ ...p, status: "closed" }));
    };
  }, [sessionId, enabled]);

  return state;
}
