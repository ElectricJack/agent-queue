import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useShellPreferences, withRightSurface } from "./useShellPreferences";
import { useSettledWidth } from "./useSettledWidth";
import { isCompactViewport, useCompactViewport } from "../hooks/useCompactViewport";

export type SurfaceKind = "pane" | "drawer" | null;
export type ActivityTab = "gates" | "events";

interface State {
  kind: SurfaceKind;
  width: number;
  activityTab: ActivityTab;
  setActivityTab: (tab: ActivityTab) => void;
  setKind: (k: SurfaceKind) => void;
  setWidth: (w: number) => void;
}

const C = createContext<State | null>(null);

const MIN = 280;
const MAX = 800;

/**
 * The right surface. Its width, and the surface and activity tab the user
 * last left open, are the user's roaming `shell_preferences` on the daemon:
 * the width is always the server's, and the last surface is reopened when the
 * dashboard loads. Which surface is open right now is this page's own state,
 * so opening the drawer on one machine does not pop it open on another.
 * Below 768 px the surface is a full-screen sheet: a compact load restores no
 * drawer, and a compact page writes no kind, tab or width (mobile dashboard D7).
 */
export function RightSurfaceProvider({ children }: { children: ReactNode }) {
  const { prefs, status, update } = useShellPreferences();
  const [kind, setKindState] = useState<SurfaceKind>(null);
  const [activityTab, setTabState] = useState<ActivityTab>("gates");
  const touched = useRef({ kind: false, tab: false });
  const restored = useRef(false);
  const compact = useCompactViewport();
  const [loadedCompact] = useState(isCompactViewport);

  // Restore once the server answers, unless a shortcut or `?openDrawer=`
  // already chose. A pane is restored by the pane store, whose bridge brings
  // the "pane" kind with it.
  useEffect(() => {
    if (status !== "ready" || restored.current) return;
    restored.current = true;
    if (loadedCompact) return; // a phone's first view is the page, not a sheet over it
    const saved = prefs.right_surface;
    if (!touched.current.tab) setTabState(saved.activity_tab);
    if (!touched.current.kind && saved.kind === "drawer") setKindState("drawer");
  }, [status, prefs.right_surface, loadedCompact]);

  const setKind = useCallback(
    (next: SurfaceKind) => {
      touched.current.kind = true;
      setKindState(next);
      if (!compact) void update(withRightSurface({ kind: next }));
    },
    [update, compact],
  );
  const setActivityTab = useCallback(
    (tab: ActivityTab) => {
      touched.current.tab = true;
      setTabState(tab);
      if (!compact) void update(withRightSurface({ activity_tab: tab }));
    },
    [update, compact],
  );
  const persistWidth = useCallback(
    (_surface: string, width: number) => (compact ? Promise.resolve() : update(withRightSurface({ width }))),
    [update, compact],
  );
  const [width, setWidth] = useSettledWidth(
    "right-surface",
    prefs.right_surface.width,
    persistWidth,
    MIN,
    MAX,
  );

  const value = useMemo<State>(
    () => ({ kind, width, setKind, setWidth, activityTab, setActivityTab }),
    [kind, width, setKind, setWidth, activityTab, setActivityTab],
  );
  return <C.Provider value={value}>{children}</C.Provider>;
}

export function useRightSurface(): State {
  const v = useContext(C);
  if (!v) throw new Error("useRightSurface called outside RightSurfaceProvider");
  return v;
}
