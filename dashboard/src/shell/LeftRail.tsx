import { Link, NavLink, useLocation } from "react-router-dom";
import {
  Squares2X2Icon,
  Cog6ToothIcon,
  FolderIcon,
} from "@heroicons/react/24/outline";
import AgentFlock from "./AgentFlock";
import { useProjects } from "../api/hooks";
import { useListNav } from "./hotkeys/useListNav";
import { workspaceNavigation, workspaceHref } from "./projectNavigation";

function linkClass(active: boolean): string {
  return `flex items-center gap-3 rounded-lg px-3 py-2 text-sm ${
    active
      ? "bg-indigo-500/15 text-indigo-200"
      : "text-gray-400 hover:bg-gray-800 hover:text-gray-100"
  }`;
}

export default function LeftRail() {
  const { data: projects } = useProjects();
  const location = useLocation();
  const { projectId, tab, isWorkspace, search } = workspaceNavigation(location);
  const navRef = useListNav<HTMLElement>({ axis: "vertical" });
  return (
    <aside className="col-start-1 row-start-2 flex h-full w-64 shrink-0 lg:w-72 flex-col overflow-hidden border-r border-gray-800 bg-gray-900">
      <nav ref={navRef} className="flex-1 space-y-6 overflow-y-auto p-3">
        <div className="space-y-0.5">
          <Link to={workspaceHref(projectId, tab, search)} data-listnav="1"
            aria-current={isWorkspace ? "page" : undefined} className={linkClass(isWorkspace)}>
            <Squares2X2Icon className="h-4 w-4" />
            <span>Command Center</span>
          </Link>
          <NavLink to="/settings" data-listnav="1" className={({ isActive }) => linkClass(isActive)}>
            <Cog6ToothIcon className="h-4 w-4" />
            <span>Settings</span>
          </NavLink>
        </div>
        <AgentFlock />
        <div>
          <p className="px-3 pb-2 text-xs uppercase text-gray-500">Projects</p>
          <div className="space-y-0.5">
            <Link to={workspaceHref(null, tab, search)} data-listnav="1"
              aria-current={isWorkspace && !projectId ? "page" : undefined}
              className={linkClass(isWorkspace && !projectId)}>
              <Squares2X2Icon className="h-4 w-4" />
              <span>All projects</span>
            </Link>
            {(projects ?? []).map((p) => (
              <Link
                key={p.id}
                to={workspaceHref(p.id, tab, search)}
                data-listnav="1"
                aria-current={projectId === p.id ? "page" : undefined}
                className={linkClass(projectId === p.id)}
              >
                <FolderIcon className="h-4 w-4" />
                <span className="truncate">{p.name ?? p.id}</span>
              </Link>
            ))}
          </div>
        </div>
      </nav>
    </aside>
  );
}
