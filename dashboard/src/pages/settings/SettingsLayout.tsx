import { Outlet } from "react-router-dom";
import SettingsSidebar from "../../components/nav/SettingsSidebar";

export default function SettingsLayout() {
  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="text-sm text-gray-500">
          Curation surfaces — everything you tell the system how to behave.
        </p>
      </header>
      <div className="flex flex-col gap-6 md:flex-row">
        <SettingsSidebar />
        <div className="min-w-0 flex-1">
          <Outlet />
        </div>
      </div>
    </div>
  );
}
