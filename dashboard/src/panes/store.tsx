import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { PANE_REGISTRY as DEFAULT_REGISTRY, type PaneEntry } from "./registry";
import { useShellPreferences, withPaneWidth, withRightSurface } from "../shell/useShellPreferences";
import { useSettledWidth } from "../shell/useSettledWidth";

export type PaneState =
  | { kind: "closed" }
  | { kind: "open"; view: string; args: unknown; width: number };

type OpenPane = { kind: "closed" } | { kind: "open"; view: string; args: unknown };

/**
 * Why the pane last changed: the server restoring the user's last pane when
 * the dashboard loads, or a call to `open` / `close` / `setArgs`. Navigation
 * history records the first against the current entry and lets only the
 * second create one.
 */
export type PaneChangeOrigin = "restore" | "call";

type Snapshot = { pane: OpenPane; origin: PaneChangeOrigin };

const DEFAULT_WIDTH = 480;
const MIN_WIDTH = 200;
const MAX_WIDTH = 800;

const INVALID = Symbol("invalid pane args");

function parseArgs(entry: PaneEntry, args: unknown): unknown {
  if (!entry.manifest.args_schema) return args;
  const parsed = entry.manifest.args_schema.safeParse(args);
  return parsed.success ? parsed.data : INVALID;
}

/** The server stores pane args as an object; anything else restores as `{}`. */
function storedArgs(args: unknown): Record<string, unknown> {
  return typeof args === "object" && args !== null && !Array.isArray(args)
    ? (args as Record<string, unknown>)
    : {};
}

interface StoreShape {
  state: PaneState;
  origin: PaneChangeOrigin;
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

/**
 * The open shell pane and its per-view widths. Both are the user's roaming
 * `shell_preferences` on the daemon: the last open pane (manifest-validated
 * `view`/`args`) is restored when the dashboard loads, and each view's width
 * is read from and written to the server. Toolbar callbacks and pane component
 * state stay in memory.
 */
export function ShellPaneProvider({ children, registryOverride }: Props) {
  const registry = registryOverride ?? DEFAULT_REGISTRY;
  const { prefs, status, update } = useShellPreferences();
  const stateRef = useRef<Snapshot>({ pane: { kind: "closed" }, origin: "restore" });
  const listeners = useRef(new Set<() => void>());
  // Once the user, a URL or an agent has chosen a pane, a restore that
  // arrives later must not replace it.
  const touched = useRef(false);
  const restored = useRef(false);

  const emit = useCallback(() => listeners.current.forEach((l) => l()), []);

  const subscribe = useCallback((l: () => void) => {
    listeners.current.add(l);
    return () => {
      listeners.current.delete(l);
    };
  }, []);
  const getSnapshot = useCallback(() => stateRef.current, []);
  const { pane, origin } = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  const persistPane = useCallback(
    (next: { view: string; args: Record<string, unknown> } | null) => {
      void update(withRightSurface({ pane: next }));
    },
    [update],
  );

  useEffect(() => {
    if (status !== "ready" || restored.current) return;
    restored.current = true;
    const saved = prefs.right_surface;
    if (touched.current || saved.kind !== "pane" || !saved.pane) return;
    const entry = registry[saved.pane.view];
    const args = entry ? parseArgs(entry, saved.pane.args ?? {}) : INVALID;
    // An unregistered view or args its manifest now rejects: stay closed, and
    // the next pane change overwrites the stale value.
    if (args === INVALID) return;
    stateRef.current = { pane: { kind: "open", view: saved.pane.view, args }, origin: "restore" };
    emit();
  }, [status, prefs.right_surface, registry, emit]);

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
      touched.current = true;
      stateRef.current = { pane: { kind: "open", view, args }, origin: "call" };
      emit();
      persistPane({ view, args: storedArgs(args) });
    },
    [registry, emit, persistPane],
  );

  const close = useCallback(() => {
    touched.current = true;
    stateRef.current = { pane: { kind: "closed" }, origin: "call" };
    emit();
    persistPane(null);
  }, [emit, persistPane]);

  const setArgs = useCallback(
    (next: unknown) => {
      const current = stateRef.current.pane;
      if (current.kind !== "open") return;
      const entry = registry[current.view];
      if (!entry) return;
      if (entry.manifest.args_schema) {
        const parsed = entry.manifest.args_schema.safeParse(next);
        if (!parsed.success) {
          throw new Error(`setArgs validation failed for view ${current.view}`);
        }
        next = parsed.data;
      }
      touched.current = true;
      stateRef.current = { pane: { ...current, args: next }, origin: "call" };
      emit();
      persistPane({ view: current.view, args: storedArgs(next) });
    },
    [registry, emit, persistPane],
  );

  const view = pane.kind === "open" ? pane.view : null;
  const persistWidth = useCallback(
    (id: string, width: number) => update(withPaneWidth(id, width)),
    [update],
  );
  const [width, setWidth] = useSettledWidth(
    view,
    (view !== null ? prefs.pane_widths[view] : undefined) ?? DEFAULT_WIDTH,
    persistWidth,
    MIN_WIDTH,
    MAX_WIDTH,
  );
  const state = useMemo<PaneState>(
    () => (pane.kind === "open" ? { ...pane, width } : pane),
    [pane, width],
  );

  const value = useMemo<StoreShape>(
    () => ({ state, origin, open, close, setArgs, setWidth, registry }),
    [state, origin, open, close, setArgs, setWidth, registry],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useShellPaneStore(): StoreShape {
  const v = useContext(Ctx);
  if (!v) throw new Error("useShellPaneStore called outside ShellPaneProvider");
  return v;
}
