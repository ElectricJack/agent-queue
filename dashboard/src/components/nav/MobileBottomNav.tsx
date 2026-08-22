import { NavLink } from "react-router-dom";
import {
  ChatBubbleLeftRightIcon,
  Squares2X2Icon,
  BriefcaseIcon,
  Cog6ToothIcon,
} from "@heroicons/react/24/outline";

const tabs = [
  { to: "/", label: "Chat", icon: ChatBubbleLeftRightIcon, end: true },
  { to: "/command-center", label: "Center", icon: Squares2X2Icon },
  { to: "/work", label: "Work", icon: BriefcaseIcon },
  { to: "/settings", label: "Settings", icon: Cog6ToothIcon },
];

export default function MobileBottomNav() {
  return (
    <nav className="fixed inset-x-0 bottom-0 z-40 flex border-t border-gray-800 bg-gray-900 md:hidden">
      {tabs.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          className={({ isActive }) =>
            `flex flex-1 flex-col items-center gap-0.5 py-2 text-xs ${
              isActive ? "text-indigo-400" : "text-gray-500"
            }`
          }
        >
          <Icon className="h-5 w-5" />
          <span>{label}</span>
        </NavLink>
      ))}
    </nav>
  );
}
