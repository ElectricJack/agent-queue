// SSE subscription hook wrapping GET /api/streams/{streamId}/subscribe.
// Follows dashboard/src/ws/useTranscriptStream.ts's bounded-buffer
// EventSource shape, plus replay-from-after_seq + reconnect-with-backoff
// (spec §7.1-§7.2) that the transcript hook doesn't need.

import { useCallback, useEffect, useRef, useState } from "react";

export interface ConsoleLine {
  seq: number;
  stream: "stdout" | "stderr";
  text: string;
  ts: number;
}

export type ConsoleStreamStatus = "connecting" | "running" | "exited" | "killed" | "error";

export interface ConsoleStreamState {
  status: ConsoleStreamStatus;
  exitCode: number | null;
  lines: ConsoleLine[];
  startedAt: number | null;
  endedAt: number | null;
  errorMessage: string | null;
  truncated: boolean;
}

interface RawFrame {
  type: "line" | "exit" | "killed";
  seq: number;
  stream?: "stdout" | "stderr";
  text?: string;
  rc?: number;
  ts: number;
  truncated?: boolean;
}

const MAX_LINES = 5000;
// Fallback only. The real budget is server-owned config
// (`streams.client_reconnect_attempts`), fetched from the stream metadata
// response below; this is what we use until that lands, or if it fails.
const DEFAULT_MAX_RECONNECT_ATTEMPTS = 5;
const BASE_BACKOFF_MS = 500;

function apiBase(): string {
  return (
    (import.meta.env.VITE_API_URL as string | undefined) ||
    `${window.location.protocol}//${window.location.host}`
  );
}

const INITIAL_STATE: ConsoleStreamState = {
  status: "connecting",
  exitCode: null,
  lines: [],
  startedAt: null,
  endedAt: null,
  errorMessage: null,
  truncated: false,
};

export function useConsoleStream(streamId: string | null | undefined): ConsoleStreamState {
  const [state, setState] = useState<ConsoleStreamState>(INITIAL_STATE);
  const afterSeqRef = useRef(-1);
  const attemptRef = useRef(0);
  const maxAttemptsRef = useRef(DEFAULT_MAX_RECONNECT_ATTEMPTS);
  const esRef = useRef<EventSource | null>(null);
  const closedRef = useRef(false);

  const appendLine = useCallback((frame: RawFrame) => {
    setState((prev) => {
      const nextLines =
        prev.lines.length >= MAX_LINES
          ? prev.lines.slice(prev.lines.length - MAX_LINES + 1)
          : prev.lines.slice();
      nextLines.push({
        seq: frame.seq,
        stream: frame.stream ?? "stdout",
        text: frame.text ?? "",
        ts: frame.ts,
      });
      return {
        ...prev,
        status: "running",
        lines: nextLines,
        startedAt: prev.startedAt ?? frame.ts,
        truncated: prev.truncated || !!frame.truncated,
      };
    });
  }, []);

  useEffect(() => {
    closedRef.current = false;
    setState(INITIAL_STATE);
    afterSeqRef.current = -1;
    attemptRef.current = 0;
    maxAttemptsRef.current = DEFAULT_MAX_RECONNECT_ATTEMPTS;
    if (!streamId) return;

    // GET /api/streams/{id} carries the daemon's configured reconnect budget.
    // Best-effort: a failure just leaves the default in place, and the value
    // is only read once the first connection error fires.
    if (typeof fetch === "function") {
      void fetch(`${apiBase()}/api/streams/${encodeURIComponent(streamId)}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((meta) => {
          const n = meta?.client_reconnect_attempts;
          if (typeof n === "number" && Number.isFinite(n) && n >= 0) {
            maxAttemptsRef.current = n;
          }
        })
        .catch(() => {
          /* keep DEFAULT_MAX_RECONNECT_ATTEMPTS */
        });
    }

    function connect() {
      const url = `${apiBase()}/api/streams/${encodeURIComponent(streamId!)}/subscribe?after_seq=${afterSeqRef.current}`;
      const es = new EventSource(url);
      esRef.current = es;

      es.onopen = () => {
        attemptRef.current = 0;
        setState((prev) => ({
          ...prev,
          status: prev.status === "error" ? "running" : prev.status,
          errorMessage: null,
        }));
      };

      es.onmessage = (msg: MessageEvent) => {
        let frame: RawFrame;
        try {
          frame = JSON.parse(msg.data);
        } catch {
          return;
        }
        afterSeqRef.current = frame.seq;
        if (frame.type === "line") {
          appendLine(frame);
        } else if (frame.type === "exit") {
          setState((prev) => ({ ...prev, status: "exited", exitCode: frame.rc ?? null, endedAt: frame.ts }));
          es.close();
        } else if (frame.type === "killed") {
          setState((prev) => ({ ...prev, status: "killed", endedAt: frame.ts }));
          es.close();
        }
      };

      es.onerror = () => {
        es.close();
        if (closedRef.current) return;
        setState((prev) =>
          prev.status === "exited" || prev.status === "killed" ? prev : { ...prev, status: "error" },
        );
        if (attemptRef.current >= maxAttemptsRef.current) {
          setState((prev) => ({ ...prev, errorMessage: "connection lost" }));
          return;
        }
        const delay = BASE_BACKOFF_MS * 2 ** attemptRef.current;
        attemptRef.current += 1;
        window.setTimeout(() => {
          if (!closedRef.current) connect();
        }, delay);
      };
    }

    connect();
    return () => {
      closedRef.current = true;
      esRef.current?.close();
      esRef.current = null;
    };
  }, [streamId, appendLine]);

  return state;
}
