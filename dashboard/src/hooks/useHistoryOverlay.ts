import { useCallback, useEffect, useRef } from "react";
import { useLocation, useNavigate, useNavigationType } from "react-router-dom";

type OverlayState = Record<string, unknown> & { overlay?: string };

/**
 * History entries an overlay was open on when something navigated away from
 * inside it. Back to one of them shows the page with the overlay closed —
 * the menu does not spring open again. Module memory: it dies with the tab.
 */
const consumed = new Set<string>();

/**
 * Open state for a drawer or sheet, kept in the current history entry (mobile
 * dashboard §4.1: drawer and sheet opens are history-aware). `show()` pushes an
 * entry whose state names the overlay, so a phone's Back closes it; `hide()`
 * goes back over that entry. Nothing is stored anywhere else.
 */
export function useHistoryOverlay(key: string) {
  const location = useLocation();
  const navigate = useNavigate();
  const navigationType = useNavigationType();
  const state = (location.state ?? null) as OverlayState | null;
  const open = state?.overlay === key && !consumed.has(location.key);
  const openedOn = useRef<string | null>(null);

  useEffect(() => {
    if (open) {
      openedOn.current = location.key;
      return;
    }
    if (openedOn.current && openedOn.current !== location.key && navigationType !== "POP") {
      consumed.add(openedOn.current);
    }
    openedOn.current = null;
  }, [open, location.key, navigationType]);

  const show = useCallback(() => {
    if (open) return;
    navigate(
      { pathname: location.pathname, search: location.search, hash: location.hash },
      { state: { ...(state ?? {}), overlay: key } },
    );
  }, [open, navigate, location.pathname, location.search, location.hash, state, key]);

  const hide = useCallback(() => {
    if (open) navigate(-1);
  }, [open, navigate]);

  return { open, show, hide };
}
