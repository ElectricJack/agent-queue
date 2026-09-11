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
import {
  UNSAFE_NavigationContext,
  useLocation,
  useNavigate,
  useNavigationType,
} from "react-router-dom";
import { useShellPaneStore, type PaneState } from "../panes/store";
import { useProjects } from "../api/hooks";
import { useShortcut } from "./hotkeys/useShortcuts";
import {
  BrowserHistoryContext,
  readBrowserEntryView,
  writeBrowserEntryView,
  type EntryView,
} from "./historyState";
import { viewTitle, type TitleNames } from "./viewTitle";

type Pane = EntryView["pane"];

/** One known entry of this tab's dashboard history. */
interface TrailEntry {
  key: string;
  pathname: string;
  search: string;
  pane: Pane;
}

interface Trail {
  /** Indexed by depth. A hole is an entry from before a reload, whose view is unknown. */
  entries: Array<TrailEntry | undefined>;
  index: number;
}

export interface NavigationHistory {
  canGoBack: boolean;
  canGoForward: boolean;
  /** What Back returns to, when known. */
  backTitle: string | null;
  forwardTitle: string | null;
  back: () => void;
  forward: () => void;
}

const INERT: NavigationHistory = {
  canGoBack: false,
  canGoForward: false,
  backTitle: null,
  forwardTitle: null,
  back: () => {},
  forward: () => {},
};

const Ctx = createContext<NavigationHistory>(INERT);

/** Back / forward through the dashboard's history; inert outside the shell. */
export function useNavigationHistory(): NavigationHistory {
  return useContext(Ctx);
}

/** Location state that acts once on arrival, and so must not follow a pane step. */
const ONE_SHOT_STATE = ["agentSelection", "restoreTaskPane"];

function carriedState(state: unknown): Record<string, unknown> | null {
  if (typeof state !== "object" || state === null) return null;
  const next = { ...(state as Record<string, unknown>) };
  for (const key of ONE_SHOT_STATE) delete next[key];
  return Object.keys(next).length > 0 ? next : null;
}

/**
 * The key of the entry the router's history is on right now, which runs
 * ahead of the rendered location while a navigation's transition is pending.
 */
function routerLocationKey(navigator: unknown): string | undefined {
  const location = (navigator as { location?: { key?: unknown } } | null)?.location;
  return typeof location?.key === "string" ? location.key : undefined;
}

function paneOf(state: PaneState): Pane {
  return state.kind === "open" ? { view: state.view, args: state.args } : null;
}

function signatureOf(pane: Pane): string {
  return pane ? JSON.stringify([pane.view, pane.args]) : "";
}

function placeEntry(trail: Trail, depth: number, entry: TrailEntry, truncate: boolean): Trail {
  const entries = truncate ? trail.entries.slice(0, depth) : trail.entries.slice();
  entries[depth] = entry;
  return { entries, index: depth };
}

/**
 * The dashboard's navigation history, owned by the shell.
 *
 * Routes are already browser history entries (the router maps every view to
 * a URL), so this does not keep a parallel stack. It fills the gap the URL
 * leaves: the shell pane. Each entry records the pane it showed — in the
 * browser's own `history.state` when the router is the window's, so it
 * survives a reload — and a Back / Forward (the top-bar buttons, the
 * shortcuts, or the browser's own buttons, mouse buttons and swipes, which all
 * arrive as the same POP) puts that pane back.
 *
 * A pane change pushes an entry of its own, at the same URL, so Back undoes
 * it; a pane change that belongs to a navigation — made in the same task as
 * the route change, such as a task pane's "Open agent terminal" closing the
 * pane, or the task pane a project switch closes — is recorded on the new
 * entry instead, so one click is one step. The server restoring the user's
 * last pane on load never creates an entry.
 */
export function NavigationHistoryProvider({ children }: { children: ReactNode }) {
  const location = useLocation();
  const navigationType = useNavigationType();
  const navigate = useNavigate();
  const paneStore = useShellPaneStore();
  const browserHistory = useContext(BrowserHistoryContext);
  const { navigator } = useContext(UNSAFE_NavigationContext);
  const { data: projects } = useProjects();

  const current = paneOf(paneStore.state);
  const signature = signatureOf(current);
  const origin = paneStore.origin;

  const paneRef = useRef(paneStore);
  paneRef.current = paneStore;
  const recorded = useRef(new Map<string, EntryView>());
  const last = useRef<{ key: string; signature: string } | null>(null);
  const index = useRef(0);
  const [trail, setTrail] = useState<Trail>({ entries: [], index: 0 });
  // True for the rest of the task in which the location changed. Effects that
  // react to a navigation (and the handler that made it) change the pane
  // within it; those changes are part of the navigation, not a step of their
  // own. Deliberately not cleared on unmount: StrictMode's simulated
  // remount would otherwise leave it set for good.
  const settling = useRef(false);
  const settleTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const read = useCallback(
    (key: string) => recorded.current.get(key) ?? readBrowserEntryView(browserHistory, key),
    [browserHistory],
  );
  const write = useCallback(
    (key: string, view: EntryView) => {
      recorded.current.set(key, view);
      writeBrowserEntryView(browserHistory, key, view);
    },
    [browserHistory],
  );

  useEffect(() => {
    const previous = last.current;
    last.current = { key: location.key, signature };
    const known = read(location.key);
    const here = { key: location.key, pathname: location.pathname, search: location.search };

    if (previous?.key !== location.key) {
      settling.current = true;
      clearTimeout(settleTimer.current);
      settleTimer.current = setTimeout(() => {
        settling.current = false;
      }, 0);

      let depth: number;
      if (!previous) depth = known?.depth ?? 0;
      else if (navigationType === "PUSH") depth = index.current + 1;
      else if (navigationType === "REPLACE") depth = index.current;
      // An entry this page load never recorded predates the feature; Back is
      // the likelier way there.
      else depth = known?.depth ?? Math.max(0, index.current - 1);

      // Put back the pane this entry showed. Applied even when it looks
      // current: an effect earlier in this commit (a project switch closing
      // the task pane) may already have changed the store.
      const restoring = navigationType === "POP" ? known : undefined;
      if (restoring) {
        if (restoring.pane) paneRef.current.open(restoring.pane.view, restoring.pane.args);
        else paneRef.current.close();
      }
      const pane = restoring ? restoring.pane : current;
      index.current = depth;
      write(location.key, { depth, pane });
      setTrail((t) => placeEntry(t, depth, { ...here, pane }, navigationType === "PUSH"));
      return;
    }

    if (previous.signature === signature) return;
    // The router applies a navigation in a transition, so a pane change made
    // alongside one — "Open agent terminal" navigates, then closes the pane —
    // renders first. The router is already on the new entry, whose arrival
    // records this pane; the entry being left keeps its own.
    const pending = routerLocationKey(navigator);
    if (pending !== undefined && pending !== location.key) return;
    if (origin === "restore" || settling.current || signatureOf(known?.pane ?? null) === signature) {
      write(location.key, { depth: index.current, pane: current });
      setTrail((t) => placeEntry(t, index.current, { ...here, pane: current }, false));
      return;
    }
    // A pane step of its own: a new entry at the same URL. The one being left
    // keeps the pane it recorded; the arrival above records this one.
    navigate(
      { pathname: location.pathname, search: location.search, hash: location.hash },
      { state: carriedState(location.state) },
    );
    // `current` is derived from `signature`, which is a dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location, navigationType, signature, origin, navigate, navigator, read, write]);

  const canGoBack = trail.index > 0;
  const canGoForward = trail.index < trail.entries.length - 1;
  const back = useCallback(() => {
    if (canGoBack) navigate(-1);
  }, [canGoBack, navigate]);
  const forward = useCallback(() => {
    if (canGoForward) navigate(1);
  }, [canGoForward, navigate]);

  // Browsers already bind these to their own Back / Forward, which arrives
  // here as a POP either way; handling them keeps them working when the
  // browser leaves the key to the page. Keys match on `event.code`, so the
  // bracket is named by its code rather than written as "[".
  useShortcut("alt+left,meta+bracketleft", {
    label: "back",
    section: "Navigation",
    onFire: back,
    when: () => canGoBack,
  });
  useShortcut("alt+right,meta+bracketright", {
    label: "forward",
    section: "Navigation",
    onFire: forward,
    when: () => canGoForward,
  });

  const registry = paneStore.registry;
  const value = useMemo<NavigationHistory>(() => {
    const names: TitleNames = {
      project: (id) => projects?.find((project) => project.id === id)?.name ?? undefined,
      pane: (view) => registry[view]?.manifest?.name,
    };
    const titleAt = (at: number) => {
      const entry = trail.entries[at];
      return entry ? viewTitle(entry, names) : null;
    };
    return {
      canGoBack,
      canGoForward,
      backTitle: canGoBack ? titleAt(trail.index - 1) : null,
      forwardTitle: canGoForward ? titleAt(trail.index + 1) : null,
      back,
      forward,
    };
  }, [trail, canGoBack, canGoForward, back, forward, projects, registry]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
