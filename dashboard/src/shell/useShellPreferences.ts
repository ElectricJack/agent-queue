import { useCallback, useMemo } from "react";
import type { RightSurface, ShellPreferences } from "../api/client";
import {
  DEFAULT_VALUES,
  useDashboardDocumentState,
  type DocumentStatus,
} from "../api/dashboardStateStore";

/**
 * The signed-in user's roaming shell preferences (`shell_preferences`, one
 * document per user on the daemon): theme, pane and right-surface widths, the
 * last right surface, the Projects disclosure, the agent-flock collapse and
 * the last project. They follow the user to every browser and machine; the
 * defaults below are shown until the daemon answers.
 */
export type ShellPrefs = Required<Omit<ShellPreferences, "right_surface">> & {
  right_surface: Required<RightSurface>;
};
export type RightSurfacePrefs = ShellPrefs["right_surface"];

export const SHELL_PREFERENCE_DEFAULTS = DEFAULT_VALUES.shell_preferences as ShellPrefs;
/** The server bounds `pane_widths` to this many views. */
export const MAX_PANE_WIDTHS = 64;

function complete(value: ShellPreferences): ShellPrefs {
  return {
    ...SHELL_PREFERENCE_DEFAULTS,
    ...value,
    right_surface: { ...SHELL_PREFERENCE_DEFAULTS.right_surface, ...value.right_surface },
  };
}

export interface ShellPreferencesState {
  prefs: ShellPrefs;
  status: DocumentStatus;
  error: Error | null;
  /** Apply a change; it is re-applied to the newest server value if that moved on. */
  update(op: (prefs: ShellPrefs) => ShellPrefs): Promise<void>;
  reset(): Promise<void>;
}

export function useShellPreferences(): ShellPreferencesState {
  const document = useDashboardDocumentState("shell_preferences");
  const prefs = useMemo(() => complete(document.value), [document.value]);
  const { update: updateDocument, reset: resetDocument } = document;
  // Changes are field patches over the loaded document rather than blind
  // whole-document writes: a write sent before the first load would otherwise
  // replace every other stored preference with its default. A failure is not
  // swallowed silently — `error`/`status` carry it to the shell's indicator,
  // and the value on screen falls back to the server's.
  const update = useCallback(
    (op: (prefs: ShellPrefs) => ShellPrefs) =>
      updateDocument((value) => op(complete(value))).catch(() => undefined),
    [updateDocument],
  );
  const reset = useCallback(() => resetDocument().catch(() => undefined), [resetDocument]);
  return { prefs, status: document.status, error: document.error, update, reset };
}

export function withRightSurface(patch: Partial<RightSurfacePrefs>) {
  return (prefs: ShellPrefs): ShellPrefs => ({
    ...prefs,
    right_surface: { ...prefs.right_surface, ...patch },
  });
}

/** Records one pane's width, keeping the most recently sized views within the bound. */
export function withPaneWidth(view: string, width: number) {
  return (prefs: ShellPrefs): ShellPrefs => {
    if (prefs.pane_widths[view] === width) return prefs;
    const rest = Object.entries(prefs.pane_widths).filter(([id]) => id !== view);
    const entries = [...rest, [view, width] as const].slice(-MAX_PANE_WIDTHS);
    return { ...prefs, pane_widths: Object.fromEntries(entries) };
  };
}
