import { NavLink } from "react-router-dom";
import {
  Squares2X2Icon,
  SignalIcon,
  BookOpenIcon,
  Cog6ToothIcon,
  CpuChipIcon,
  FolderIcon,
} from "@heroicons/react/24/outline";
import { useOrchestratorStatus, useProjects } from "../api/hooks";

type SystemLink = {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  end?: boolean;
};

const systemLinks: SystemLink[] = [
  { to: "/system", label: "Overview", icon: Squares2X2Icon, end: true },
  { to: "/system/events", label: "Events", icon: SignalIcon },
  { to: "/system/playbooks", label: "Playbooks", icon: BookOpenIcon },
  { to: "/system/config", label: "Config", icon: Cog6ToothIcon },
];

export default function Sidebar() {
  const { data: projects } = useProjects();
  const { data: orch } = useOrchestratorStatus();
  const projectList = projects ?? [];
  const orchPaused = orch?.status === "paused";

  return (
    <aside className="flex w-60 flex-col border-r border-gray-800 bg-gray-900">
      <div className="flex h-14 items-center gap-2 border-b border-gray-800 px-4">
        <CpuChipIcon className="h-6 w-6 text-indigo-400" />
        <span className="text-lg font-semibold tracking-tight">Agent Queue</span>
      </div>

      <nav className="flex-1 space-y-6 overflow-y-auto p-3">
        <SidebarSection title="System">
          {systemLinks.map(({ to, label, icon: Icon, end }) => (
            <SidebarLink
              key={to}
              to={to}
              icon={Icon}
              label={label}
              end={end}
              trailing={to === "/system" && orchPaused ? <PausedDot /> : null}
            />
          ))}
        </SidebarSection>

        <SidebarSection title="Projects">
          {projectList.length === 0 ? (
            <p className="px-3 py-1 text-xs text-gray-600">No projects.</p>
          ) : (
            projectList.map((p) => (
              <SidebarLink
                key={p.id}
                to={`/projects/${p.id}`}
                icon={FolderIcon}
                label={p.name || p.id}
                trailing={p.paused ? <PausedDot /> : null}
              />
            ))
          )}
        </SidebarSection>
      </nav>
    </aside>
  );
}

function SidebarSection({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="px-3 pb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">
        {title}
      </p>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}

function SidebarLink({
  to,
  icon: Icon,
  label,
  end,
  trailing,
}: {
  to: string;
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  end?: boolean;
  trailing?: React.ReactNode;
}) {
  return (
    <NavLink
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
      {trailing}
    </NavLink>
  );
}

function PausedDot() {
  return (
    <span title="Paused" className="ml-auto inline-block h-1.5 w-1.5 rounded-full bg-amber-400" />
  );
}

