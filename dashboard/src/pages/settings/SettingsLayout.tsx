import { NavLink, Outlet } from "react-router-dom";

// Placeholder — replaced/expanded in Phase 3 Task 5.
const tabs = [
  { to: "/settings/playbooks", label: "Playbooks" },
  { to: "/settings/profiles", label: "Profiles" },
  { to: "/settings/intelligence-classes", label: "Intelligence Classes" },
  { to: "/settings/config", label: "Config" },
];

export default function SettingsLayout() {
  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="text-sm text-gray-500">
          System-wide playbooks, profiles, intelligence classes, and config.
        </p>
      </header>
      <nav className="flex gap-2 border-b border-gray-800">
        {tabs.map((t) => (
          <NavLink
            key={t.to}
            to={t.to}
            className={({ isActive }) =>
              `px-3 py-2 text-sm ${
                isActive
                  ? "border-b-2 border-indigo-500 text-indigo-300"
                  : "text-gray-400 hover:text-gray-200"
              }`
            }
          >
            {t.label}
          </NavLink>
        ))}
      </nav>
      <div>
        <Outlet />
      </div>
    </div>
  );
}
