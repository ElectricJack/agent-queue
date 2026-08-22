import { Outlet } from "react-router-dom";

/**
 * Task-5 placeholder. Real shell (grid layout, LeftRail, TopBar,
 * RightSurface, palette, hotkeys, pane host, drawer) lands in Tasks 6-13.
 */
export default function AppShellV2() {
  return (
    <div className="flex h-screen w-screen items-center justify-center bg-gray-950 text-gray-100">
      <div className="text-center">
        <p className="text-sm text-gray-500">AppShellV2 placeholder</p>
        <p className="mt-2 text-xs text-gray-600">
          Remove <span className="font-mono">?v2=1</span> to see the current shell.
        </p>
        <div className="mt-6">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
