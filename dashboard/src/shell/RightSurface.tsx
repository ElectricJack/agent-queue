import { XMarkIcon } from "@heroicons/react/24/outline";
import { useRightSurface } from "./useRightSurface";
import ShellPaneHost from "./ShellPaneHost";
import ActivityDrawer from "./ActivityDrawer";
import { useShellPaneStore } from "../panes/store";

/**
 * Dispatches between ShellPaneHost and ActivityDrawer. Only one of the two
 * can occupy the right side at once — opening one closes the other.
 */
export default function RightSurface() {
  const { kind, width, setKind } = useRightSurface();
  const pane = useShellPaneStore();
  if (kind === null) return null;
  const label = kind === "pane" ? "Pane" : "Activity";
  return (
    <aside
      style={{ width }}
      className="row-start-2 flex h-full shrink-0 flex-col border-l border-gray-800 bg-gray-950"
    >
      <header className="flex items-center justify-between border-b border-gray-800 p-2">
        <span className="text-xs uppercase text-gray-500">{label}</span>
        <button
          onClick={() => {
            setKind(null);
            if (kind === "pane") pane.close();
          }}
          className="rounded p-1 text-gray-400 hover:bg-gray-800"
          title="Close"
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
