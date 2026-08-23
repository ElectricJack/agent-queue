import { useState } from "react";
import { useShellPaneStore } from "../panes/store";
import type { PaneToolbarAction, ShortcutBinding } from "../panes/types";

/**
 * Renders whichever pane view is currently open in the shell-pane store.
 * Delegates to the registered `Component`, giving it callbacks to close,
 * mutate its args, publish a toolbar row, and publish local shortcuts.
 */
export default function ShellPaneHost() {
  const { state, close, setArgs } = useShellPaneStore();
  const [toolbar, setToolbar] = useState<PaneToolbarAction[]>([]);
  // Pane-published shortcuts. Real wiring into `useShortcut` happens once
  // per-view plans land; the setter still needs to be a stable no-op so
  // views can call it without exploding.
  const [, setShortcuts] = useState<ShortcutBinding[]>([]);

  if (state.kind !== "open") {
    return (
      <div className="p-4 text-xs text-gray-500">No pane open.</div>
    );
  }
  const { registry } = useShellPaneStore();
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
      <div className="flex-1 overflow-auto p-3">
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
