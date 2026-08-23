import { useEffect, useRef } from "react";

interface Opts {
  axis?: "vertical" | "horizontal";
  loop?: boolean;
}

/**
 * Attach arrow-key navigation to a container. Any focusable child with
 * `data-listnav="1"` participates. Enter/Space activate via click().
 * ArrowDown/Right → next, ArrowUp/Left → prev, Home/End jump.
 */
export function useListNav<T extends HTMLElement>(opts: Opts = {}) {
  const ref = useRef<T | null>(null);
  const { axis = "vertical", loop = true } = opts;
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const next = axis === "vertical" ? "ArrowDown" : "ArrowRight";
    const prev = axis === "vertical" ? "ArrowUp" : "ArrowLeft";
    const onKey = (e: KeyboardEvent) => {
      const items = Array.from(
        el.querySelectorAll<HTMLElement>('[data-listnav="1"]'),
      );
      if (items.length === 0) return;
      const active = document.activeElement as HTMLElement | null;
      const idx = active ? items.indexOf(active) : -1;
      let target = -1;
      if (e.key === next) target = idx < 0 ? 0 : idx + 1;
      else if (e.key === prev) target = idx < 0 ? items.length - 1 : idx - 1;
      else if (e.key === "Home") target = 0;
      else if (e.key === "End") target = items.length - 1;
      else return;
      if (loop) target = ((target % items.length) + items.length) % items.length;
      else target = Math.max(0, Math.min(items.length - 1, target));
      e.preventDefault();
      items[target]?.focus();
    };
    el.addEventListener("keydown", onKey);
    return () => el.removeEventListener("keydown", onKey);
  }, [axis, loop]);
  return ref;
}
