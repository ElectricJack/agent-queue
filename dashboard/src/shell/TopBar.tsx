import { ArrowLeftIcon, ArrowRightIcon, CommandLineIcon, BellIcon } from "@heroicons/react/24/outline";
import { useRightSurface } from "./useRightSurface";
import { useShellPaneStore } from "../panes/store";
import { useAllOpenGates } from "../api/hooks";
import { usePaletteState } from "./palette/paletteState";
import { useShellPreferences } from "./useShellPreferences";
import { useNavigationHistory } from "./navigationHistory";
import { detectPlatform } from "./hotkeys/usePlatform";

const HISTORY_BUTTON =
  "rounded p-2 text-gray-400 hover:bg-gray-800 disabled:cursor-default disabled:opacity-30 disabled:hover:bg-transparent";

export default function TopBar() {
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
    <header className="col-span-3 row-start-1 flex h-12 shrink-0 items-center justify-between border-b border-gray-800 bg-gray-950 px-4">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold">Agent Q</span>
      </div>
      <div className="flex items-center gap-1">
        {preferenceProblem && (
          <span role="status" title={preferenceProblem.detail}
            className="rounded px-2 py-1 text-[11px] text-amber-300">
            {preferenceProblem.label}
          </span>
        )}
        <button
          type="button"
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
          className={HISTORY_BUTTON}
          title={history.canGoForward
            ? `Forward to ${history.forwardTitle ?? "the next view"} (${forwardKey})`
            : "Forward"}
        >
          <ArrowRightIcon className="h-4 w-4" />
        </button>
        <button
          onClick={palette.toggle}
          className="rounded p-2 text-gray-400 hover:bg-gray-800"
          title="Command palette (Cmd/Ctrl-K)"
        >
          <CommandLineIcon className="h-4 w-4" />
        </button>
        <button
          onClick={toggleDrawer}
          className="relative rounded p-2 text-gray-400 hover:bg-gray-800"
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
