/**
 * SSE hook wrapping `GET /api/sessions/{session_id}/stream`.
 *
 * The server (src/api/sessions.py) sends `data:` frames in two shapes:
 *   - transcript: {source:"transcript", uuid, parent_uuid, type, text, model, usage, ts}
 *   - peek:       {source:"peek", text, ts}
 * Plus periodic ": heartbeat" comments which EventSource discards.
 *
 * The hook keeps entries in a bounded buffer (default 2000) so long-running
 * sessions don't grow unboundedly. Reconnect is handled by EventSource
 * natively; we surface the `readyState` as ConnectionStatus for the UI.
 */

import { useEffect, useRef, useState, useCallback } from "react";

export type TranscriptSource = "transcript" | "peek";

export interface TranscriptFrame {
  source: TranscriptSource;
  uuid?: string;
  parent_uuid?: string | null;
  type?: string;
  text: string;
  model?: string | null;
  usage?: unknown;
  ts: number;
  // Locally assigned monotonic index so React keys stay stable when a frame
  // has no uuid (peek fallback).
  _idx: number;
}

export type StreamStatus = "connecting" | "open" | "closed" | "error";

interface UseTranscriptStreamOptions {
  bufferSize?: number;
  enabled?: boolean;
}

const DEFAULT_BUFFER = 2000;

export function useTranscriptStream(
  sessionId: string | null | undefined,
  opts: UseTranscriptStreamOptions = {},
) {
  const { bufferSize = DEFAULT_BUFFER, enabled = true } = opts;
  const [entries, setEntries] = useState<TranscriptFrame[]>([]);
  const [status, setStatus] = useState<StreamStatus>("closed");
  const [error, setError] = useState<string | null>(null);
  const idxRef = useRef(0);
  const esRef = useRef<EventSource | null>(null);

  const clear = useCallback(() => {
    setEntries([]);
    idxRef.current = 0;
  }, []);

  useEffect(() => {
    if (!enabled || !sessionId) return;

    const base =
      import.meta.env.VITE_API_URL ||
      `${window.location.protocol}//${window.location.host}`;
    const url = `${base}/api/sessions/${encodeURIComponent(sessionId)}/stream`;

    setStatus("connecting");
    setError(null);
    const es = new EventSource(url);
    esRef.current = es;

    es.onopen = () => setStatus("open");

    es.onmessage = (msg) => {
      try {
        const raw = JSON.parse(msg.data) as Omit<TranscriptFrame, "_idx">;
        const frame: TranscriptFrame = { ...raw, _idx: idxRef.current++ };
        setEntries((prev) => {
          const next = prev.length >= bufferSize
            ? prev.slice(prev.length - bufferSize + 1)
            : prev.slice();
          next.push(frame);
          return next;
        });
      } catch {
        // Ignore malformed frame; server also emits comment heartbeats
        // which EventSource never surfaces to onmessage anyway.
      }
    };

    es.onerror = () => {
      setStatus("error");
      setError("stream error (EventSource will retry)");
    };

    return () => {
      es.close();
      esRef.current = null;
      setStatus("closed");
    };
  }, [sessionId, enabled, bufferSize]);

  return { entries, status, error, clear };
}
