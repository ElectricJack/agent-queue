/**
 * The input-only terminal socket for a phone: it types into the session and
 * never attaches, so the agent's tmux window keeps its size. Open only while
 * `enabled` (the Type toggle); switching back to watching closes it.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { connectTerminal, type TerminalConnection, type TerminalConnectionState } from "./terminalSocket";
import { encodeEntry, encodeKey, ENTER, SUBMIT_DELAY_MS, type TerminalKey } from "../components/terminalInput";

export interface TerminalInput {
  /** `closed` while watching. */
  state: TerminalConnectionState | { status: "closed"; message?: undefined; attempt?: undefined };
  connected: boolean;
  /** Send one entry, then Enter as its own write. */
  sendEntry(text: string): void;
  sendKey(key: TerminalKey | string): void;
  reconnect(): void;
}

type Step = { bytes: Uint8Array; pause: number };

export function useTerminalInput(sessionId: string, enabled: boolean): TerminalInput {
  const [state, setState] = useState<TerminalInput["state"]>({ status: "closed" });
  const connection = useRef<TerminalConnection | null>(null);
  // Writes go out in order; the pause after an entry holds back later keys too.
  const steps = useRef<Step[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const reset = useCallback(() => {
    steps.current = [];
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, []);

  useEffect(() => {
    if (!enabled) return;
    const current = connectTerminal({
      sessionId,
      mode: "input",
      // Nothing arrives on an input-only socket; acknowledge defensively.
      write: (_bytes, processed) => processed(),
      onState: (next) => {
        // Input queued for a connection that dropped is discarded, never replayed.
        if (next.status !== "connected") reset();
        setState(next);
      },
    });
    connection.current = current;
    return () => {
      current.close();
      connection.current = null;
      reset();
      setState({ status: "closed" });
    };
  }, [sessionId, enabled, reset]);

  const pump = useCallback(() => {
    while (timer.current === null && steps.current.length) {
      const step = steps.current.shift()!;
      connection.current?.sendInput(step.bytes);
      if (step.pause) {
        timer.current = setTimeout(() => {
          timer.current = null;
          pump();
        }, step.pause);
      }
    }
  }, []);

  const connected = state.status === "connected";
  const sendEntry = useCallback((text: string) => {
    if (!connected) return;
    const bytes = encodeEntry(text);
    if (bytes.byteLength) steps.current.push({ bytes, pause: SUBMIT_DELAY_MS });
    steps.current.push({ bytes: encodeKey(ENTER), pause: 0 });
    pump();
  }, [connected, pump]);
  const sendKey = useCallback((key: TerminalKey | string) => {
    if (!connected) return;
    steps.current.push({ bytes: encodeKey(key), pause: 0 });
    pump();
  }, [connected, pump]);
  const reconnect = useCallback(() => connection.current?.reconnect(), []);

  // Enabled but not yet reported: the socket is on its way.
  const shown: TerminalInput["state"] = !enabled ? { status: "closed" } : state.status === "closed" ? { status: "connecting" } : state;
  return { state: shown, connected: enabled && connected, sendEntry, sendKey, reconnect };
}
