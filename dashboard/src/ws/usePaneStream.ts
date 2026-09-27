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
import { useCallback, useEffect, useRef, useState } from "react";
import { sessionShow } from "../api/client";
import { reconnectLoop } from "./reconnect";

export type PaneStatus = "connecting" | "reconnecting" | "open" | "stopped" | "error" | "closed";

export interface PaneState {
  screen: string | null;
  status: PaneStatus;
  error: string | null;
  seq: number;
  attempt?: number;
  /** Browser time (ms) of the last screen frame; null before the first. */
  lastFrameAt: number | null;
  /** From a dropped stream until frames flow again: the screen on show may be stale. */
  interrupted: boolean;
}

interface Options {
  enabled?: boolean;
}

/** One pathological frame cannot grow memory past this (the hook holds one screen). */
export const MAX_SCREEN_CHARS = 200_000;

const INITIAL: PaneState = {
  screen: null,
  status: "closed",
  error: null,
  seq: 0,
  lastFrameAt: null,
  interrupted: false,
};

// Keep the tail: a terminal's newest lines are at the bottom.
const bounded = (screen: string) =>
  screen.length > MAX_SCREEN_CHARS ? screen.slice(-MAX_SCREEN_CHARS) : screen;

export function usePaneStream(
  sessionId: string | null | undefined,
  opts: Options = {},
): PaneState & { reconnect: () => void } {
  const { enabled = true } = opts;
  const [state, setState] = useState<PaneState>(INITIAL);
  // Bumped by a manual reconnect that has no pending retry to hurry along.
  const [generation, setGeneration] = useState(0);
  const recoverRef = useRef<(() => boolean) | null>(null);
  const lastSession = useRef<string | null | undefined>(undefined);

  useEffect(() => {
    if (!enabled || !sessionId) return;

    const base =
      import.meta.env.VITE_API_URL ||
      `${window.location.protocol}//${window.location.host}`;
    const url = `${base}/api/sessions/${encodeURIComponent(sessionId)}/pane`;

    // A manual reconnect (or re-enable) of the same session keeps the last
    // screen on show, still marked interrupted until a frame arrives; a
    // different session starts blank.
    const sameSession = lastSession.current === sessionId;
    lastSession.current = sessionId;
    setState((prev) =>
      sameSession
        ? { ...prev, status: "connecting", error: null, interrupted: prev.screen !== null }
        : { ...INITIAL, status: "connecting" },
    );
    let es: EventSource | undefined;
    let probe: AbortController | undefined;
    const retry = reconnectLoop(open, (attempt) => setState((p) => ({
      ...p, status: "reconnecting", error: null, attempt, interrupted: true,
    })));
    recoverRef.current = retry.recover;
    // Terminal frames end the stream for good. The server returns from its
    // generator on one, and per the SSE spec a browser RECONNECTS a
    // normally-closed stream after ~3s — which would re-subscribe, spawn a
    // fresh poll loop, and peek a reaped tmux session for an empty screen,
    // flapping the pane between "Session ended" and blank forever.
    let done = false;

    async function diagnose() {
      const controller = new AbortController();
      probe = controller;
      const timeout = setTimeout(() => controller.abort(), 5_000);
      try {
        const { data } = await sessionShow({
          baseUrl: base, body: { session_id: sessionId! }, signal: controller.signal,
          throwOnError: true,
        });
        if (done || probe !== controller || controller.signal.aborted) return;
        if (data?.session && ["stopped", "sleeping", "quarantined"].includes(data.session.state ?? "")) {
          done = true;
          retry.stop();
          setState((p) => ({ ...p, status: "stopped" }));
        }
      } catch (error) {
        if (done || probe !== controller || controller.signal.aborted) return;
        if (error instanceof Error && /^API (400|401|403|404|409):/.test(error.message)) {
          done = true;
          retry.stop();
          setState((p) => ({ ...p, status: "error", error: error.message }));
        }
      } finally {
        clearTimeout(timeout);
        if (probe === controller) {
          probe = undefined;
          if (!done) retry.resume();
        }
      }
    }

    function open() {
      if (done) return;
      probe?.abort();
      probe = undefined;
      es?.close();
      const current = new EventSource(url);
      es = current;
      const live = () => !done && es === current;
      current.onopen = () => {
        if (!live()) return;
        retry.connected();
        setState((p) => ({ ...p, status: "open", error: null, attempt: undefined }));
      };

      current.onmessage = (msg) => {
        if (!live()) return;
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
          retry.stop();
          current.close();
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
          const incoming = f.screen != null ? bounded(f.screen) : prev.screen;
          return {
            screen: incoming,
            status: "open",
            error: null,
            seq,
            lastFrameAt: Date.now(),
            interrupted: false,
          };
        });
      };

      current.onerror = () => {
        if (!live()) return;
        // Own the retry even when EventSource enters CLOSED (HTTP handshake
        // failure). Close its native retry to avoid duplicate subscriptions.
        const closed = current.readyState === 2;
        current.close();
        es = undefined;
        retry.retry(closed);
        if (closed) void diagnose();
      };
    }
    retry.start();

    return () => {
      done = true;
      recoverRef.current = null;
      retry.stop();
      probe?.abort();
      probe = undefined;
      es?.close();
      setState((p) => ({ ...p, status: "closed" }));
    };
  }, [sessionId, enabled, generation]);

  /**
   * Reconnect now: a pending retry runs at once; otherwise (an error frame, a
   * refused stream, a connect that hangs) a fresh stream opens. Either way the
   * last screen stays on show.
   */
  const reconnect = useCallback(() => {
    if (!recoverRef.current?.()) setGeneration((n) => n + 1);
  }, []);
  return { ...state, reconnect };
}
