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

const isTerminal = (s: PaneStatus) => s === "stopped" || s === "error";

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
    // Terminal frames end the stream for good. The server returns from its
    // generator on one, and per the SSE spec a browser RECONNECTS a
    // normally-closed stream after ~3s — which would re-subscribe, spawn a
    // fresh poll loop, and peek a reaped tmux session for an empty screen,
    // flapping the pane between "Session ended" and blank forever.
    let done = false;

    es.onopen = () => setState((p) => (done ? p : { ...p, status: "open" }));

    es.onmessage = (msg) => {
      let f: {
        type?: string;
        screen?: string;
        message?: string;
        seq?: number;
      };
      try {
        f = JSON.parse(msg.data);
      } catch {
        return; // Malformed frame; heartbeats are comments and never land here.
      }
      if (f.type === "stopped" || f.type === "error") {
        done = true;
        es.close();
      }
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
        // A screen frame arriving after a terminal state must never wipe the
        // last good screen with an empty one ("" is not nullish, so `??`
        // would let it through); the banner has nothing to sit above then.
        const incoming = f.screen ?? prev.screen;
        if (isTerminal(prev.status))
          return { ...prev, screen: incoming || prev.screen, seq };
        return { screen: incoming, status: "open", error: null, seq };
      });
    };

    es.onerror = () => {
      if (done) return; // our own close(), not a failure
      setState((p) => ({
        ...p,
        status: "error",
        error:
          es.readyState === 2 // CLOSED — EventSource gave up for good
            ? "stream closed (no reconnect)"
            : "stream interrupted — reconnecting…",
      }));
    };

    return () => {
      done = true;
      es.close();
      esRef.current = null;
      setState((p) => ({ ...p, status: "closed" }));
    };
  }, [sessionId, enabled]);

  return state;
}
