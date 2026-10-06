import { Suspense, useEffect, useRef, useState } from "react";
import { Outlet, useSearchParams } from "react-router-dom";
import LeftRail from "./LeftRail";
import TopBar from "./TopBar";
import ProviderAvailabilityBanner from "./ProviderAvailabilityBanner";
import RightSurface from "./RightSurface";
import { RightSurfaceProvider, useRightSurface } from "./useRightSurface";
import { ShortcutsProvider, useShortcut } from "./hotkeys/useShortcuts";
import CheatSheetModal from "./hotkeys/CheatSheetModal";
import { useShellPaneStore } from "../panes/store";
import { ActionRegistryProvider, useRegisterAction } from "./palette/registerActions";
import { useShellPreferences } from "./useShellPreferences";
import { PaletteStateProvider } from "./palette/paletteState";
import { Palette } from "./palette/Palette";
import { useAgentPushBridge } from "../panes/agentPush";
import { useNavigate } from "react-router-dom";
import { NavigationHistoryProvider } from "./navigationHistory";
import ReviewToasts from "./ReviewToasts";
import ProviderUsageBars from "./ProviderUsageBars";
import { canFocusTerminal } from "../components/terminalFocus";
import { useCompactViewport } from "../hooks/useCompactViewport";
import { useHistoryOverlay } from "../hooks/useHistoryOverlay";
import { useMediaQuery } from "../hooks/useMediaQuery";

/**
 * Reads `?openDrawer=events|gates` on route entry, opens the drawer,
 * then strips the param so refresh doesn't re-open it forever. True while
 * the param is still in the rendered URL.
 */
function useOpenDrawerParam(): boolean {
  const [params, setParams] = useSearchParams();
  const { setKind, setActivityTab } = useRightSurface();
  const pane = useShellPaneStore();
  useEffect(() => {
    const v = params.get("openDrawer");
    if (v !== "events" && v !== "gates") return;
    if (pane.state.kind === "open") pane.close();
    setActivityTab(v);
    setKind("drawer");
    const next = new URLSearchParams(params);
    next.delete("openDrawer");
    setParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params.get("openDrawer")]);
  const pending = params.get("openDrawer");
  return pending === "events" || pending === "gates";
}

/** Keeps the right-surface kind in sync with the pane store's state. */
function usePaneSurfaceBridge() {
  const pane = useShellPaneStore();
  const { kind, setKind } = useRightSurface();
  useEffect(() => {
    if (pane.state.kind === "open" && kind !== "pane") setKind("pane");
    if (pane.state.kind === "closed" && kind === "pane") setKind(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pane.state.kind]);
}

/** Two-key section jumps; g h opens the supervisor terminal and g a opens the flock. */
function useSectionJumps() {
  const navigate = useNavigate();
  const [pending, setPending] = useState(false);
  useShortcut("g", {
    label: "goto (start prefix)",
    section: "Navigation",
    onFire: () => {
      setPending(true);
      window.setTimeout(() => setPending(false), 1200);
    },
  });
  useShortcut("h", {
    label: "goto supervisor terminal",
    section: "Navigation",
    onFire: () => {
      if (!pending) return;
      setPending(false);
      navigate("/agents?agent=supervisor-global", { state: {
        agentSelection: "replace",
        ...(canFocusTerminal() ? { terminalFocus: "supervisor-global" } : {}),
      } });
    },
    when: () => pending,
  });
  useShortcut("a", {
    label: "goto agent flock",
    section: "Navigation",
    onFire: () => {
      if (!pending) return;
      setPending(false);
      navigate("/agents");
    },
    when: () => pending,
  });
  useShortcut("c", {
    label: "goto command center",
    section: "Navigation",
    onFire: () => {
      if (!pending) return;
      setPending(false);
      navigate("/command-center");
    },
    when: () => pending,
  });
  useShortcut("s", {
    label: "goto settings",
    section: "Navigation",
    onFire: () => {
      if (!pending) return;
      setPending(false);
      navigate("/settings");
    },
    when: () => pending,
  });
  useShortcut("p", {
    label: "goto projects",
    section: "Navigation",
    onFire: () => {
      if (!pending) return;
      setPending(false);
      navigate("/command-center");
    },
    when: () => pending,
  });
  return pending;
}

/**
 * Publishes the user's theme preference on the root element (the shell is
 * styled dark today; a theme stylesheet or selector keys off `data-theme`) and
 * offers resetting every shell preference to its default from the palette.
 */
function useShellPreferenceControls() {
  const { prefs, reset } = useShellPreferences();
  const prefersLight = useMediaQuery("(prefers-color-scheme: light)");
  const theme = prefs.theme === "system" ? (prefersLight ? "light" : "dark") : prefs.theme;
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  useRegisterAction({
    id: "shell.reset-preferences",
    label: "Reset shell preferences",
    section: "Preferences",
    keywords: ["defaults", "layout", "theme", "width"],
    run: () => void reset(),
  });
}

function ShellBody() {
  useAgentPushBridge();
  usePaneSurfaceBridge();
  const drawerParamPending = useOpenDrawerParam();
  useShellPreferenceControls();
  const gotoPending = useSectionJumps();
  const [cheat, setCheat] = useState(false);
  const pane = useShellPaneStore();
  const rs = useRightSurface();

  useShortcut("?", {
    label: "toggle cheat sheet",
    section: "Help",
    onFire: () => setCheat((v) => !v),
  });
  useShortcut("[", {
    label: "toggle pane surface",
    section: "Surfaces",
    onFire: () => {
      if (pane.state.kind === "open") pane.close();
      else if (rs.kind !== "pane") pane.open("__stub-smoke", { text: "hello" });
    },
  });
  useShortcut("]", {
    label: "toggle activity drawer",
    section: "Surfaces",
    onFire: () => {
      if (rs.kind === "drawer") rs.setKind(null);
      else {
        if (rs.kind === "pane") pane.close();
        rs.setKind("drawer");
      }
    },
  });
  useShortcut("Escape", {
    label: "close right surface",
    section: "Surfaces",
    onFire: () => {
      if (rs.kind === "pane") pane.close();
      rs.setKind(null);
    },
    when: () => rs.kind !== null,
  });

  // Below 768 px (mobile dashboard §4.1) the rail is a drawer and the right
  // surface a full-screen sheet; both opens are history entries, so Back
  // closes them.
  const compact = useCompactViewport();
  const rail = useHistoryOverlay("rail");
  const activitySheet = useHistoryOverlay("activity");
  const previousKind = useRef(rs.kind);
  // The activity drawer gets its own entry when it opens, and closing it
  // leaves that entry. Only the drawer closing does: a pane that replaced it
  // is a navigation-history step of its own, and a navigation from inside it
  // has already left the entry. The router applies navigations in a
  // transition, so wait for a pending `?openDrawer=` strip to render before
  // pushing the entry over it.
  useEffect(() => {
    const was = previousKind.current;
    previousKind.current = rs.kind;
    if (!compact || drawerParamPending) return;
    if (rs.kind === "drawer" && !activitySheet.open) activitySheet.show();
    if (was === "drawer" && rs.kind === null && activitySheet.open) activitySheet.hide();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compact, rs.kind, drawerParamPending]);
  // Back (or any navigation) off the drawer's entry closes the drawer.
  useEffect(() => {
    if (compact && !activitySheet.open && rs.kind === "drawer") rs.setKind(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activitySheet.open]);

  return (
    <div className="app-viewport grid w-full grid-cols-1 grid-rows-[auto_1fr] bg-gray-950 text-gray-100 md:grid-cols-[auto_1fr_auto]">
      {/* The outage banner rides in the header row so it spans every page
          without shifting the rail/main/surface grid beneath it. */}
      <div className="col-span-full row-start-1 flex min-w-0 flex-col pt-safe px-safe">
        <TopBar center={<ProviderUsageBars />} onOpenMenu={compact ? rail.show : undefined} />
        <ProviderAvailabilityBanner />
      </div>
      {compact ? rail.open && <LeftRail variant="drawer" onClose={rail.hide} /> : <LeftRail />}
      <main className="col-start-1 row-start-2 min-h-0 min-w-0 overflow-hidden pb-safe px-safe md:col-start-2">
        <Suspense
          fallback={
            <div className="flex h-full items-center justify-center text-sm text-gray-500">
              Loading…
            </div>
          }
        >
          <Outlet />
        </Suspense>
      </main>
      <RightSurface compact={compact} />
      <Palette />
      <ReviewToasts />
      <CheatSheetModal open={cheat} onClose={() => setCheat(false)} />
      {gotoPending && (
        <div className="pointer-events-none fixed bottom-4 left-1/2 -translate-x-1/2 rounded bg-gray-800/90 px-3 py-1.5 text-xs text-gray-200 shadow">
          Go to: <kbd className="font-mono">h</kbd> supervisor · <kbd>a</kbd> flock · <kbd>c</kbd> cc ·{" "}
          <kbd>s</kbd> settings · <kbd>p</kbd> projects
        </div>
      )}
    </div>
  );
}

export default function AppShellV2() {
  // ShellPaneProvider is hoisted to the top of App.tsx (both v1 and v2
  // need it — v1's CommandCenter dispatches to the pane too). Don't
  // wrap here again — a second provider would shadow the outer, and
  // the outer subscribers (including v1 code paths reachable via
  // navigation) would see stale state.
  return (
    <ShortcutsProvider>
      <PaletteStateProvider>
        <ActionRegistryProvider>
          <RightSurfaceProvider>
            <NavigationHistoryProvider>
              <ShellBody />
            </NavigationHistoryProvider>
          </RightSurfaceProvider>
        </ActionRegistryProvider>
      </PaletteStateProvider>
    </ShortcutsProvider>
  );
}
