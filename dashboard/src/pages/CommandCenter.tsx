import { NavLink, Outlet, useLocation, useParams } from "react-router-dom";
import { useListNav } from "../shell/hotkeys/useListNav";
import { PROJECT_TABS, TASK_TABS, isTaskTab, projectNavigation, workspaceHref } from "../shell/projectNavigation";
import ProjectHeader from "./project/ProjectLayout";
import { TaskWorkspaceProvider } from "./command-center/TaskWorkspace";
import TaskToolbar from "./command-center/TaskToolbar";

function tabClass(active: boolean): string {
  return `whitespace-nowrap border-b-2 px-3 py-2 text-sm ${
    active
      ? "border-indigo-400 text-indigo-200"
      : "border-transparent text-gray-400 hover:text-gray-200"
  }`;
}

/** The same workspace serves all projects and the selected project's resources. */
export default function CommandCenter() {
  const { projectId } = useParams();
  const location = useLocation();
  const { tab } = projectNavigation(location.pathname);
  const showTasks = isTaskTab(tab);
  const tabs = projectId ? [...TASK_TABS, ...PROJECT_TABS] : TASK_TABS;
  const tabRef = useListNav<HTMLElement>({ axis: "horizontal" });
  return (
    <TaskWorkspaceProvider>
      <div className="flex h-full min-h-0 flex-col">
        {projectId ? <ProjectHeader key={`header:${projectId}`} /> : (
          <header className="shrink-0 px-4 py-3">
            <h1 className="text-lg font-semibold">Command Center</h1>
            <p className="text-xs text-gray-500">All projects</p>
          </header>
        )}
        <nav ref={tabRef} aria-label="Command Center views"
          className="flex shrink-0 overflow-x-auto border-b border-gray-800 bg-gray-950 px-4">
          {tabs.map(({ tab: target, label }) => (
            <NavLink key={target} to={workspaceHref(projectId, target, location.search)} end
              data-listnav="1" className={({ isActive }) => tabClass(isActive)}>
              {label}
            </NavLink>
          ))}
        </nav>
        {showTasks && <TaskToolbar />}
        {/* Keep URL filters in the provider; reset project-owned dialogs/forms. */}
        <div key={`content:${projectId ?? "all"}`} className={showTasks
          ? "min-h-0 flex-1 overflow-hidden"
          : "min-h-0 flex-1 overflow-y-auto p-6"}>
          <Outlet />
        </div>
      </div>
    </TaskWorkspaceProvider>
  );
}
