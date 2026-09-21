import { useLayoutEffect, useRef, useState } from "react";
import { useLocation, useNavigationType } from "react-router-dom";
import { useShellPaneStore } from "../panes/store";
import type { PaneToolbarAction, ShortcutBinding } from "../panes/types";

/**
 * Where each pane was scrolled, per history entry and pane, so Back / Forward
 * return to the same place. Module memory: a reload starts at the top.
 */
const paneScroll = new Map<string, number>();
const PANE_SCROLL_LIMIT = 200;

function rememberScroll(key: string, top: number) {
  paneScroll.delete(key);
  paneScroll.set(key, top);
  if (paneScroll.size > PANE_SCROLL_LIMIT) {
    const oldest = paneScroll.keys().next().value;
    if (oldest !== undefined) paneScroll.delete(oldest);
  }
}

/**
 * Renders whichever pane view is currently open in the shell-pane store.
 * Delegates to the registered `Component`, giving it callbacks to close,
 * mutate its args, publish a toolbar row, and publish local shortcuts.
 */
export default function ShellPaneHost() {
  const { state, close, setArgs, registry } = useShellPaneStore();
  const [toolbar, setToolbar] = useState<PaneToolbarAction[]>([]);
  // Pane-published shortcuts. Real wiring into `useShortcut` happens once
  // per-view plans land; the setter still needs to be a stable no-op so
  // views can call it without exploding.
  const [, setShortcuts] = useState<ShortcutBinding[]>([]);
  const location = useLocation();
  const navigationType = useNavigationType();
  const scroller = useRef<HTMLDivElement | null>(null);
  const scrollKey = state.kind === "open"
    ? `${location.key}|${JSON.stringify([state.view, state.args])}`
    : null;
  useLayoutEffect(() => {
    if (!scrollKey || navigationType !== "POP" || !scroller.current) return;
    scroller.current.scrollTop = paneScroll.get(scrollKey) ?? 0;
  }, [scrollKey, navigationType]);

  if (state.kind !== "open") {
    return (
      <div className="p-4 text-xs text-gray-500">No pane open.</div>
    );
  }
  const entry = registry[state.view];
  if (!entry) {
    return (
      <div className="p-4 text-xs text-red-400">
        Unknown pane view: <span className="font-mono">{state.view}</span>
      </div>
    );
  }
  const { Component, manifest } = entry;
  const Icon = manifest.icon;
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-gray-800 p-2">
        <Icon className="h-4 w-4 text-gray-400" />
        <span className="text-xs font-medium text-gray-200">{manifest.name}</span>
        <div className="ml-auto flex items-center gap-1">
          {toolbar.map((a) => (
            <button
              key={a.id}
              onClick={a.onClick}
              disabled={a.disabled}
              title={a.label}
              className="rounded p-1 text-xs text-gray-400 hover:bg-gray-800 disabled:opacity-40"
            >
              {a.icon ? <a.icon className="h-3.5 w-3.5" /> : a.label}
            </button>
          ))}
        </div>
      </div>
      <div
        ref={scroller}
        data-testid="shell-pane-scroller"
        onScroll={(event) => {
          if (scrollKey) rememberScroll(scrollKey, event.currentTarget.scrollTop);
        }}
        className="flex-1 overflow-auto p-3"
      >
        <Component
          args={state.args}
          close={close}
          setArgs={setArgs}
          setToolbar={setToolbar}
          setShortcuts={setShortcuts}
        />
      </div>
    </div>
  );
}
