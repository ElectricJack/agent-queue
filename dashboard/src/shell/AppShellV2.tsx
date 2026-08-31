import { Suspense, useEffect, useState } from "react";
import { Outlet, useSearchParams } from "react-router-dom";
import LeftRail from "./LeftRail";
import TopBar from "./TopBar";
import RightSurface from "./RightSurface";
import { RightSurfaceProvider, useRightSurface } from "./useRightSurface";
import { ShortcutsProvider, useShortcut } from "./hotkeys/useShortcuts";
import CheatSheetModal from "./hotkeys/CheatSheetModal";
import { useShellPaneStore } from "../panes/store";
import { ActionRegistryProvider } from "./palette/registerActions";
import { PaletteStateProvider } from "./palette/paletteState";
import { Palette } from "./palette/Palette";
import { useAgentPushBridge } from "../panes/agentPush";
import { useNavigate } from "react-router-dom";

/**
 * Reads `?openDrawer=events|gates` on route entry, opens the drawer,
 * then strips the param so refresh doesn't re-open it forever.
 */
function useOpenDrawerParam() {
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
      navigate("/agents?agent=supervisor-global", { state: { agentSelection: "replace" } });
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

function ShellBody() {
  useAgentPushBridge();
  usePaneSurfaceBridge();
  useOpenDrawerParam();
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

  return (
    <div className="grid h-screen w-screen grid-cols-[auto_1fr_auto] grid-rows-[auto_1fr] bg-gray-950 text-gray-100">
      <TopBar />
      <LeftRail />
      <main className="col-start-2 row-start-2 min-h-0 min-w-0 overflow-hidden">
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
      <RightSurface />
      <Palette />
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
            <ShellBody />
          </RightSurfaceProvider>
        </ActionRegistryProvider>
      </PaletteStateProvider>
    </ShortcutsProvider>
  );
}
