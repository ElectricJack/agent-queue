import { useCallback, useEffect, useRef, useState } from "react";

/** A resize gesture is persisted once it has been still for this long. */
export const WIDTH_SETTLE_MS = 250;

/**
 * A persisted width that follows a resize gesture locally and writes once the
 * gesture settles, so dragging a divider is one server write rather than one
 * per pointer move (dashboard-state contract §10.4). After the write lands —
 * or fails — the persisted value is shown again.
 *
 * `key` names what is being sized (a pane view id); a gesture on one key never
 * leaks into another, and a `null` key ignores `setWidth`.
 */
export function useSettledWidth(
  key: string | null,
  persisted: number,
  persist: (key: string, width: number) => Promise<unknown>,
  min: number,
  max: number,
): [number, (width: number) => void] {
  const [gesture, setGesture] = useState<{ key: string; width: number } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  const setWidth = useCallback(
    (width: number) => {
      if (key === null) return;
      const current = { key, width: Math.max(min, Math.min(max, Math.round(width))) };
      setGesture(current);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        void persist(current.key, current.width).finally(() =>
          setGesture((shown) => (shown === current ? null : shown)),
        );
      }, WIDTH_SETTLE_MS);
    },
    [key, persist, min, max],
  );

  return [gesture !== null && gesture.key === key ? gesture.width : persisted, setWidth];
}
