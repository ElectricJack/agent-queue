import { useEffect, useRef, type RefObject } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function focusableIn(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => !el.hasAttribute("hidden") && el.getAttribute("aria-hidden") !== "true",
  );
}

export interface FocusTrapOptions {
  /** Called on Escape while the trap is active. */
  onEscape?: () => void;
  /** Return focus to whatever held it before the trap activated. Default true. */
  restoreFocus?: boolean;
}

/**
 * Keep Tab / Shift+Tab inside `container` while `active`, move focus into it
 * when it activates, close on Escape, and hand focus back on deactivation
 * (mobile dashboard §4.1: drawers and sheets trap focus and return it).
 */
export function useFocusTrap(
  container: RefObject<HTMLElement | null>,
  active: boolean,
  { onEscape, restoreFocus = true }: FocusTrapOptions = {},
) {
  const escape = useRef(onEscape); // callers pass inline functions; do not re-trap per render
  useEffect(() => {
    escape.current = onEscape;
  });
  useEffect(() => {
    const el = container.current;
    if (!active || !el) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!el.contains(document.activeElement)) el.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && escape.current) {
        e.preventDefault();
        e.stopPropagation();
        escape.current();
        return;
      }
      if (e.key !== "Tab") return;
      const items = focusableIn(el);
      if (items.length === 0) {
        e.preventDefault();
        el.focus();
        return;
      }
      const first = items[0]!;
      const last = items[items.length - 1]!;
      const active = document.activeElement as HTMLElement | null;
      const inside = active !== null && el.contains(active);
      if (e.shiftKey) {
        if (!inside || active === first || active === el) {
          e.preventDefault();
          last.focus();
        }
      } else if (!inside || active === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      if (restoreFocus && previous?.isConnected) previous.focus();
    };
  }, [container, active, restoreFocus]);
}
