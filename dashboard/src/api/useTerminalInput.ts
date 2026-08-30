import { useCallback, useEffect, useRef, useState } from "react";
import { sessionInput } from "./client";

export type TerminalWrite = { text: string; key?: never } | { key: string; text?: never };
type InputLease = { sessionId: string; active: boolean; failed: boolean };

// All views of the same session share ordering. A failed or unmounted view
// invalidates its lease; queued keys (especially Enter) must never be replayed.
const queues = new Map<string, Promise<void>>();

export function useTerminalInput(sessionId: string | null | undefined, enabled: boolean) {
  const leaseRef = useRef<InputLease | null>(null);
  const lastSession = useRef(sessionId);
  const errorRef = useRef<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(0);

  useEffect(() => {
    if (lastSession.current !== sessionId) {
      lastSession.current = sessionId;
      errorRef.current = null;
      setError(null);
    }
    leaseRef.current = enabled && sessionId
      ? { sessionId, active: true, failed: !!errorRef.current }
      : null;
    setPending(0);
    return () => {
      if (leaseRef.current) leaseRef.current.active = false;
      leaseRef.current = null;
    };
  }, [sessionId, enabled]);

  const write = useCallback((input: TerminalWrite) => {
    const lease = leaseRef.current;
    if (!enabled || !sessionId || !lease?.active || lease.failed || lease.sessionId !== sessionId) return;
    if (input.text !== undefined && !input.text) return;
    if (input.text !== undefined && new TextEncoder().encode(input.text).byteLength > 65536) {
      lease.failed = true;
      errorRef.current = "Paste exceeds the 64 KiB limit. No partial text was sent.";
      setError(errorRef.current);
      return;
    }
    setPending((count) => count + 1);
    const queued: Promise<void> = (queues.get(sessionId) ?? Promise.resolve()).then(async () => {
      if (!lease.active || lease.failed) return;
      try {
        // Direct SDK call: input is never retried or routed through messaging.
        await sessionInput({ body: { session_id: sessionId, ...input }, throwOnError: true });
      } catch (err) {
        if (!lease.active) return;
        lease.failed = true;
        errorRef.current = err instanceof Error ? err.message : "Terminal input failed.";
        setError(errorRef.current);
      }
    }).finally(() => {
      if (lease.active) setPending((count) => Math.max(0, count - 1));
      if (queues.get(sessionId) === queued) queues.delete(sessionId);
    });
    queues.set(sessionId, queued);
  }, [enabled, sessionId]);

  const resume = () => {
    if (!enabled || !sessionId) return;
    // A new lease allows fresh input, never unsent keys from the failed lease.
    if (leaseRef.current) leaseRef.current.active = false;
    leaseRef.current = { sessionId, active: true, failed: false };
    errorRef.current = null;
    setError(null);
    setPending(0);
  };

  return { write, resume, error, pending };
}
