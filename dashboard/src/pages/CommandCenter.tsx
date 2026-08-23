import { NavLink, Outlet } from "react-router-dom";
import { useListNav } from "../shell/hotkeys/useListNav";

const tabs = [
  { to: "/command-center/graph?v2=1", label: "Graph" },
  { to: "/command-center/tasks?v2=1", label: "Tasks" },
  { to: "/command-center/agents?v2=1", label: "Agents" },
];

function tabClass(active: boolean): string {
  return `border-b-2 px-3 py-2 text-sm ${
    active
      ? "border-indigo-400 text-indigo-200"
      : "border-transparent text-gray-400 hover:text-gray-200"
  }`;
}

/**
 * Command Center hub — three tabs (Graph, Tasks, Agents) sharing the same
 * project-strip context (persisted in localStorage by each tab that uses it).
 * Route redirects and pane dispatches live in App.tsx.
 */
export default function CommandCenter() {
  const tabRef = useListNav<HTMLDivElement>({ axis: "horizontal" });
  return (
    <div className="flex h-full flex-col">
      <div
        ref={tabRef}
        className="flex border-b border-gray-800 bg-gray-950 px-4"
      >
        {tabs.map((t) => (
          <NavLink
            key={t.to}
            to={t.to}
            end
            data-listnav="1"
            className={({ isActive }) => tabClass(isActive)}
          >
            {t.label}
          </NavLink>
        ))}
      </div>
      <div className="flex-1 overflow-hidden">
        <Outlet />
      </div>
    </div>
  );
}
