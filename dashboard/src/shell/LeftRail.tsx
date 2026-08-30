import { NavLink } from "react-router-dom";
import {
  ChatBubbleLeftRightIcon,
  Squares2X2Icon,
  Cog6ToothIcon,
  FolderIcon,
} from "@heroicons/react/24/outline";
import AgentFlock from "./AgentFlock";
import { useProjects } from "../api/hooks";
import { useListNav } from "./hotkeys/useListNav";

const sections = [
  { to: "/", label: "Home", icon: ChatBubbleLeftRightIcon, end: true },
  { to: "/command-center", label: "Command Center", icon: Squares2X2Icon, end: false },
  { to: "/settings", label: "Settings", icon: Cog6ToothIcon, end: false },
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
  const navRef = useListNav<HTMLElement>({ axis: "vertical" });
  return (
    <aside className="col-start-1 row-start-2 flex h-full w-64 shrink-0 lg:w-72 flex-col overflow-hidden border-r border-gray-800 bg-gray-900">
      <nav ref={navRef} className="flex-1 space-y-6 overflow-y-auto p-3">
        <div className="space-y-0.5">
          {sections.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end} data-listnav="1" className={({ isActive }) => linkClass(isActive)}>
              <Icon className="h-4 w-4" />
              <span>{label}</span>
            </NavLink>
          ))}
        </div>
        <AgentFlock />
        <div>
          <p className="px-3 pb-2 text-xs uppercase text-gray-500">Projects</p>
          <div className="space-y-0.5">
            {(projects ?? []).map((p) => (
              <NavLink
                key={p.id}
                to={`/projects/${p.id}`}
                data-listnav="1"
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
