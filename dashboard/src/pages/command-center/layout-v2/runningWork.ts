import { useSyncExternalStore } from "react";
import type { RunningTarget } from "../../../api/graphLayout";

// The toolbar and canvas are siblings.  Keep one transient jump identity out
// of the URL: `focus` remains the durable navigation scope, while this only
// survives long enough for the matching frame to load and pan.
let target: RunningTarget | null = null;
let notice: string | null = null;
const targetListeners = new Set<() => void>();
const noticeListeners = new Set<() => void>();

function publish(listeners: Set<() => void>) {
  for (const listener of [...listeners]) listener();
}

export function publishRunningWorkJump(next: RunningTarget | null) {
  target = next;
  publish(targetListeners);
}

export function clearRunningWorkJump() {
  publishRunningWorkJump(null);
}

export function useRunningWorkJump(): RunningTarget | null {
  return useSyncExternalStore(
    (listener) => { targetListeners.add(listener); return () => targetListeners.delete(listener); },
    () => target,
  );
}

export function publishRunningWorkNotice(next: string | null) {
  notice = next;
  publish(noticeListeners);
}

export function useRunningWorkNotice(): string | null {
  return useSyncExternalStore(
    (listener) => { noticeListeners.add(listener); return () => noticeListeners.delete(listener); },
    () => notice,
  );
}
