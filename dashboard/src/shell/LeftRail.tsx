import { NavLink } from "react-router-dom";
import {
  ChatBubbleLeftRightIcon,
  Squares2X2Icon,
  Cog6ToothIcon,
  FolderIcon,
} from "@heroicons/react/24/outline";
import { useProjects } from "../api/hooks";

const sections = [
  { to: "/?v2=1", label: "Home", icon: ChatBubbleLeftRightIcon, end: true },
  { to: "/command-center?v2=1", label: "Command Center", icon: Squares2X2Icon, end: false },
  { to: "/settings?v2=1", label: "Settings", icon: Cog6ToothIcon, end: false },
];

function linkClass(active: boolean): string {
  return `flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${
    active
      ? "bg-indigo-500/15 text-indigo-200"
      : "text-gray-400 hover:bg-gray-800 hover:text-gray-100"
  }`;
}

export default function LeftRail() {
  const { data: projects } = useProjects();
  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-gray-800 bg-gray-900">
      <nav className="flex-1 space-y-6 overflow-y-auto p-3">
        <div className="space-y-0.5">
          {sections.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} className={({ isActive }) => linkClass(isActive)}>
              <Icon className="h-4 w-4" />
              <span>{label}</span>
            </NavLink>
          ))}
        </div>
        <div>
          <p className="px-3 pb-2 text-xs uppercase text-gray-500">Projects</p>
          <div className="space-y-0.5">
            {(projects ?? []).map((p) => (
              <NavLink
                key={p.id}
                to={`/projects/${p.id}?v2=1`}
                className={({ isActive }) => linkClass(isActive)}
              >
                <FolderIcon className="h-4 w-4" />
                <span className="truncate">{p.name ?? p.id}</span>
              </NavLink>
            ))}
          </div>
        </div>
      </nav>
    </aside>
  );
}
