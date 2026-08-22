import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { PANE_REGISTRY as DEFAULT_REGISTRY, type PaneEntry } from "./registry";

export type PaneState =
  | { kind: "closed" }
  | { kind: "open"; view: string; args: unknown; width: number };

const DEFAULT_WIDTH = 480;
const MIN_WIDTH = 200;
const MAX_WIDTH = 800;

function widthKey(viewId: string): string {
  return `aq:shellpane:width:${viewId}`;
}

function loadWidth(viewId: string): number {
  try {
    const raw = localStorage.getItem(widthKey(viewId));
    const n = raw ? Number.parseInt(raw, 10) : NaN;
    if (Number.isFinite(n) && n >= MIN_WIDTH && n <= MAX_WIDTH) return n;
  } catch {
    /* localStorage unavailable */
  }
  return DEFAULT_WIDTH;
}

function persistWidth(viewId: string, width: number): void {
  try {
    localStorage.setItem(widthKey(viewId), String(width));
  } catch {
    /* localStorage unavailable */
  }
}

interface StoreShape {
  state: PaneState;
  open: (view: string, args: unknown) => void;
  close: () => void;
  setArgs: (next: unknown) => void;
  setWidth: (n: number) => void;
  registry: Record<string, PaneEntry>;
}

const Ctx = createContext<StoreShape | null>(null);

interface Props {
  children: ReactNode;
  /** Overridable for tests. */
  registryOverride?: Record<string, PaneEntry>;
}

export function ShellPaneProvider({ children, registryOverride }: Props) {
  const registry = registryOverride ?? DEFAULT_REGISTRY;
  const stateRef = useRef<PaneState>({ kind: "closed" });
  const listeners = useRef(new Set<() => void>());

  const emit = () => listeners.current.forEach((l) => l());

  const subscribe = useCallback((l: () => void) => {
    listeners.current.add(l);
    return () => {
      listeners.current.delete(l);
    };
  }, []);
  const getSnapshot = useCallback(() => stateRef.current, []);
  const state = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const open = useCallback(
    (view: string, args: unknown) => {
      const entry = registry[view];
      if (!entry) {
        console.error(`ShellPane: unknown view id "${view}"`);
        return;
      }
      if (entry.manifest.args_schema) {
        const parsed = entry.manifest.args_schema.safeParse(args);
        if (!parsed.success) {
          console.error(
            `ShellPane: args validation failed for view ${view}`,
            parsed.error.format(),
          );
          return;
        }
        args = parsed.data;
      }
      stateRef.current = {
        kind: "open",
        view,
        args,
        width: loadWidth(view),
      };
      emit();
    },
    [registry],
  );

  const close = useCallback(() => {
    stateRef.current = { kind: "closed" };
    emit();
  }, []);

  const setArgs = useCallback(
    (next: unknown) => {
      if (stateRef.current.kind !== "open") return;
      const entry = registry[stateRef.current.view];
      if (!entry) return;
      if (entry.manifest.args_schema) {
        const parsed = entry.manifest.args_schema.safeParse(next);
        if (!parsed.success) {
          throw new Error(
            `setArgs validation failed for view ${stateRef.current.view}`,
          );
        }
        next = parsed.data;
      }
      stateRef.current = { ...stateRef.current, args: next };
      emit();
    },
    [registry],
  );

  const setWidth = useCallback((n: number) => {
    if (stateRef.current.kind !== "open") return;
    const clamped = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, n));
    persistWidth(stateRef.current.view, clamped);
    stateRef.current = { ...stateRef.current, width: clamped };
    emit();
  }, []);

  const value = useMemo<StoreShape>(
    () => ({ state, open, close, setArgs, setWidth, registry }),
    [state, open, close, setArgs, setWidth, registry],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useShellPaneStore(): StoreShape {
  const v = useContext(Ctx);
  if (!v) throw new Error("useShellPaneStore called outside ShellPaneProvider");
  return v;
}
