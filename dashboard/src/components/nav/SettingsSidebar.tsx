import { NavLink } from "react-router-dom";
import {
  BookOpenIcon,
  UserGroupIcon,
  CpuChipIcon,
  Cog6ToothIcon,
  FolderIcon,
} from "@heroicons/react/24/outline";
import { useListNav } from "../../shell/hotkeys/useListNav";

const links = [
  { to: "playbooks", label: "Playbooks", icon: BookOpenIcon },
  { to: "profiles", label: "Profiles", icon: UserGroupIcon },
  { to: "intelligence-classes", label: "Intelligence Classes", icon: CpuChipIcon },
  { to: "project-roots", label: "Project roots", icon: FolderIcon },
  { to: "config", label: "Config", icon: Cog6ToothIcon },
];

export default function SettingsSidebar() {
  const navRef = useListNav<HTMLElement>({ axis: "vertical" });
  return (
    <div className="relative shrink-0 md:w-56">
      <aside ref={navRef} className="flex gap-1 overflow-x-auto border-b border-gray-800 pb-2 md:flex-col md:border-b-0 md:border-r md:pr-4">
        {links.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            data-listnav="1"
            className={({ isActive }) =>
              `flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium whitespace-nowrap ${
                isActive
                  ? "bg-indigo-500/10 text-indigo-400"
                  : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
              }`
            }
          >
            <Icon className="h-4 w-4 shrink-0" />
            <span>{label}</span>
          </NavLink>
        ))}
      </aside>
      {/* Right-edge fade so mobile users can tell the tab strip scrolls further. */}
      <div className="pointer-events-none absolute top-0 right-0 bottom-2 w-8 bg-gradient-to-l from-gray-950 to-transparent md:hidden" />
    </div>
  );
}
