import { useRef } from "react";
import { XMarkIcon } from "@heroicons/react/24/outline";
import { useRightSurface } from "./useRightSurface";
import ShellPaneHost from "./ShellPaneHost";
import ActivityDrawer from "./ActivityDrawer";
import { useShellPaneStore } from "../panes/store";
import { useFocusTrap } from "../hooks/useFocusTrap";

/**
 * Dispatches between ShellPaneHost and ActivityDrawer. Only one of the two
 * can occupy the right side at once — opening one closes the other. Below
 * 768 px it is a full-screen, focus-trapping sheet whose roaming width is
 * ignored (mobile dashboard §4.1); Escape is the shell's own shortcut. At
 * 768 px and up a wide roaming width is capped so the rail and 20rem of page
 * still fit beside it, and the page never scrolls sideways.
 */
export default function RightSurface({ compact = false }: { compact?: boolean }) {
  const { kind, width, setKind } = useRightSurface();
  const pane = useShellPaneStore();
  const panel = useRef<HTMLElement>(null);
  useFocusTrap(panel, compact && kind !== null);
  if (kind === null) return null;
  const label = kind === "pane" ? "Pane" : "Activity";
  const close = () => {
    setKind(null);
    if (kind === "pane") pane.close();
  };
  return (
    <aside
      ref={panel}
      tabIndex={compact ? -1 : undefined}
      role={compact ? "dialog" : undefined}
      aria-modal={compact || undefined}
      aria-label={compact ? label : undefined}
      style={compact ? undefined : { width }}
      className={compact
        ? "fixed inset-0 z-40 flex flex-col overflow-hidden bg-gray-950 pt-safe pb-safe px-safe"
        : "col-start-3 row-start-2 flex h-full max-w-[calc(100vw-36rem)] shrink-0 flex-col overflow-hidden border-l border-gray-800 bg-gray-950 lg:max-w-[calc(100vw-38rem)]"}
    >
      <header className="flex items-center justify-between border-b border-gray-800 p-2">
        <span className="text-xs uppercase text-gray-500">{label}</span>
        <button
          type="button"
          data-primary-control
          aria-label={`Close ${label.toLowerCase()}`}
          title="Close"
          onClick={close}
          className="inline-flex items-center justify-center rounded p-1 text-gray-400 hover:bg-gray-800"
        >
          <XMarkIcon className="h-4 w-4" />
        </button>
      </header>
      <div className="flex-1 overflow-hidden">
        {kind === "pane" ? <ShellPaneHost /> : <ActivityDrawer />}
      </div>
    </aside>
  );
}
