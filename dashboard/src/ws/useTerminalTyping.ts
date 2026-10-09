/**
 * Typing from a phone into the terminal it is attached to: entries and key
 * strip keys go out in order over that attach connection, as keystrokes into
 * the phone's own tmux client.
 */
import { useCallback, useEffect, useRef, type RefObject } from "react";
import type { TerminalConnection } from "./terminalSocket";
import { encodeEntry, encodeKey, ENTER, SUBMIT_DELAY_MS, type TerminalKey } from "../components/terminalInput";

export interface TerminalTyping {
  /** Send one entry, then Enter as its own write. */
  sendEntry(text: string): void;
  sendKey(key: TerminalKey | string): void;
}

type Step = { bytes: Uint8Array; pause: number };

export function useTerminalTyping(connection: RefObject<TerminalConnection | null>, connected: boolean): TerminalTyping {
  // Writes go out in order; the pause after an entry holds back later keys too.
  const steps = useRef<Step[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Input queued for a connection that dropped is discarded, never replayed.
  useEffect(() => {
    if (connected) return;
    steps.current = [];
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, [connected]);
  useEffect(() => () => {
    if (timer.current !== null) clearTimeout(timer.current);
  }, []);

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
  }, [connection]);

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

  return { sendEntry, sendKey };
}
