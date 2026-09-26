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
import { useEffect, useState } from "react";
import { sessionShow } from "../api/client";
import { reconnectLoop } from "./reconnect";

export type PaneStatus = "connecting" | "reconnecting" | "open" | "stopped" | "error" | "closed";

export interface PaneState {
  screen: string | null;
  status: PaneStatus;
  error: string | null;
  seq: number;
  attempt?: number;
  reconnect?: () => void;
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

  useEffect(() => {
    if (!enabled || !sessionId) return;

    const base =
      import.meta.env.VITE_API_URL ||
      `${window.location.protocol}//${window.location.host}`;
    const url = `${base}/api/sessions/${encodeURIComponent(sessionId)}/pane`;

    setState({ ...INITIAL, status: "connecting" });
    let es: EventSource | undefined;
    let probe: AbortController | undefined;
    const retry = reconnectLoop(open, (attempt) => setState((p) => ({
      ...p, status: "reconnecting", error: null, attempt, reconnect: retry.recover,
    })));
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
          const incoming = f.screen ?? prev.screen;
          return { screen: incoming, status: "open", error: null, seq };
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
      retry.stop();
      probe?.abort();
      probe = undefined;
      es?.close();
      setState((p) => ({ ...p, status: "closed" }));
    };
  }, [sessionId, enabled]);

  return state;
}
