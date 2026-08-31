import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

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
const KEY = "aq:rightsurface:width";

function loadWidth(): number {
  try {
    const raw = localStorage.getItem(KEY);
    const n = raw ? Number.parseInt(raw, 10) : NaN;
    if (Number.isFinite(n) && n >= MIN && n <= MAX) return n;
  } catch {
    /* noop */
  }
  return 480;
}

export function RightSurfaceProvider({ children }: { children: ReactNode }) {
  const [kind, setKind] = useState<SurfaceKind>(null);
  const [activityTab, setActivityTab] = useState<ActivityTab>("gates");
  const [width, setWidthRaw] = useState<number>(loadWidth);
  const setWidth = useCallback((w: number) => {
    const clamped = Math.max(MIN, Math.min(MAX, w));
    setWidthRaw(clamped);
    try {
      localStorage.setItem(KEY, String(clamped));
    } catch {
      /* noop */
    }
  }, []);
  const value = useMemo<State>(
    () => ({ kind, width, setKind, setWidth, activityTab, setActivityTab }),
    [kind, width, setKind, setWidth, activityTab],
  );
  return <C.Provider value={value}>{children}</C.Provider>;
}

export function useRightSurface(): State {
  const v = useContext(C);
  if (!v) throw new Error("useRightSurface called outside RightSurfaceProvider");
  return v;
}
