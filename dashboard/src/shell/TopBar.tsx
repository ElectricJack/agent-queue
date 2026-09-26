import { ArrowLeftIcon, ArrowRightIcon, Bars3Icon, CommandLineIcon, BellIcon } from "@heroicons/react/24/outline";
import { useRightSurface } from "./useRightSurface";
import { useShellPaneStore } from "../panes/store";
import { useAllOpenGates } from "../api/hooks";
import { usePaletteState } from "./palette/paletteState";
import { useShellPreferences } from "./useShellPreferences";
import { useNavigationHistory } from "./navigationHistory";
import { detectPlatform } from "./hotkeys/usePlatform";
import type { ReactNode } from "react";

const HISTORY_BUTTON =
  "inline-flex items-center justify-center rounded p-2 text-gray-400 hover:bg-gray-800 disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent";

/**
 * The shell's top bar. `onOpenMenu` is given below 768 px, where the rail is a
 * drawer behind the "Open navigation" button; there the bar keeps its icon
 * buttons, each with an accessible name and a 44 px target (mobile dashboard
 * D10), and drops Forward, which the phone's browser already offers.
 */
export default function TopBar({ center, onOpenMenu }: { center?: ReactNode; onOpenMenu?: () => void }) {
  const { kind, setKind } = useRightSurface();
  const pane = useShellPaneStore();
  const palette = usePaletteState();
  const history = useNavigationHistory();
  const mac = detectPlatform().modifier === "cmd";
  const backKey = mac ? "⌘[" : "Alt+←";
  const forwardKey = mac ? "⌘]" : "Alt+→";
  const { data: gates } = useAllOpenGates();
  const humanGates = (gates ?? []).filter((g) => g.gate_type === "human").length;
  const preferences = useShellPreferences();
  const preferenceProblem =
    preferences.status === "unavailable"
      ? {
          label: "Preferences unavailable",
          detail: "The daemon's preference store did not answer; defaults are shown and changes are not saved.",
        }
      : preferences.error
        ? {
            label: "Preference not saved",
            detail: "The daemon did not accept the last change; the saved value is shown.",
          }
        : null;

  const toggleDrawer = () => {
    if (kind === "drawer") {
      setKind(null);
    } else {
      if (kind === "pane") pane.close();
      setKind("drawer");
    }
  };

  return (
    <header className="col-span-3 row-start-1 flex h-12 shrink-0 items-center gap-1 border-b border-gray-800 bg-gray-950 px-2 sm:gap-3 md:px-4">
      {onOpenMenu && (
        <button type="button" data-primary-control aria-label="Open navigation" title="Open navigation"
          onClick={onOpenMenu}
          className="inline-flex shrink-0 items-center justify-center rounded text-gray-300 hover:bg-gray-800">
          <Bars3Icon className="h-5 w-5" />
        </button>
      )}
      <div className="hidden shrink-0 items-center gap-2 sm:flex">
        <span className="text-sm font-semibold">Agent Q</span>
      </div>
      <div className="min-w-0 flex-1 overflow-hidden">{center}</div>
      <div className="flex shrink-0 items-center gap-1">
        {preferenceProblem && (
          <span role="status" title={preferenceProblem.detail}
            className="max-w-[8rem] truncate rounded px-2 py-1 text-[11px] text-amber-300">
            {preferenceProblem.label}
          </span>
        )}
        <button
          type="button"
          data-primary-control
          aria-label="Back"
          onClick={history.back}
          disabled={!history.canGoBack}
          className={HISTORY_BUTTON}
          title={history.canGoBack
            ? `Back to ${history.backTitle ?? "the previous view"} (${backKey})`
            : "Back"}
        >
          <ArrowLeftIcon className="h-4 w-4" />
        </button>
        <button
          type="button"
          aria-label="Forward"
          onClick={history.forward}
          disabled={!history.canGoForward}
          className={`${HISTORY_BUTTON} max-md:hidden`}
          title={history.canGoForward
            ? `Forward to ${history.forwardTitle ?? "the next view"} (${forwardKey})`
            : "Forward"}
        >
          <ArrowRightIcon className="h-4 w-4" />
        </button>
        <button
          type="button"
          data-primary-control
          aria-label="Command palette"
          onClick={palette.toggle}
          className="inline-flex items-center justify-center rounded p-2 text-gray-400 hover:bg-gray-800"
          title="Command palette (Cmd/Ctrl-K)"
        >
          <CommandLineIcon className="h-4 w-4" />
        </button>
        <button
          type="button"
          data-primary-control
          aria-label="Activity"
          onClick={toggleDrawer}
          className="relative inline-flex items-center justify-center rounded p-2 text-gray-400 hover:bg-gray-800"
          title="Activity drawer (])"
        >
          <BellIcon className="h-4 w-4" />
          {humanGates > 0 && (
            <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-medium text-white">
              {humanGates}
            </span>
          )}
        </button>
      </div>
    </header>
  );
}
