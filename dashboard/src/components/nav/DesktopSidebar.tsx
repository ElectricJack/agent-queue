import { NavLink } from "react-router-dom";
import {
  ChatBubbleLeftRightIcon,
  Squares2X2Icon,
  BriefcaseIcon,
  Cog6ToothIcon,
  CpuChipIcon,
  FolderIcon,
} from "@heroicons/react/24/outline";
import { useOrchestratorStatus, useProjects } from "../../api/hooks";

const sections = [
  { to: "/", label: "Chat", icon: ChatBubbleLeftRightIcon, end: true },
  { to: "/command-center", label: "Command Center", icon: Squares2X2Icon },
  { to: "/work", label: "Work", icon: BriefcaseIcon },
  { to: "/settings", label: "Settings", icon: Cog6ToothIcon },
];

export default function DesktopSidebar() {
  const { data: projects } = useProjects();
  const { data: orch } = useOrchestratorStatus();
  const paused = orch?.status === "paused";
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-gray-800 bg-gray-900 md:flex">
      <div className="flex h-14 items-center gap-2 border-b border-gray-800 px-4">
        <CpuChipIcon className="h-6 w-6 text-indigo-400" />
        <span className="text-lg font-semibold tracking-tight">Agent Queue</span>
        {paused && (
          <span title="Orchestrator paused" className="ml-auto h-2 w-2 rounded-full bg-amber-400" />
        )}
      </div>
      <nav className="flex-1 space-y-6 overflow-y-auto p-3">
        <div className="space-y-0.5">
          {sections.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                  isActive
                    ? "bg-indigo-500/10 text-indigo-400"
                    : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                }`
              }
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span className="flex-1 truncate">{label}</span>
            </NavLink>
          ))}
        </div>
        <div>
          <p className="px-3 pb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
            Projects
          </p>
          <div className="space-y-0.5">
            {(projects ?? []).length === 0 && (
              <p className="px-3 py-1 text-xs text-gray-600">No projects.</p>
            )}
            {(projects ?? []).map((p) => (
              <NavLink
                key={p.id}
                to={`/chat/${p.id}`}
                className={({ isActive }) =>
                  `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                    isActive
                      ? "bg-indigo-500/10 text-indigo-400"
                      : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                  }`
                }
              >
                <FolderIcon className="h-4 w-4 shrink-0" />
                <span className="flex-1 truncate">{p.name || p.id}</span>
                {p.paused && <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />}
              </NavLink>
            ))}
          </div>
        </div>
      </nav>
    </aside>
  );
}
