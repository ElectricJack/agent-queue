import { Link, NavLink, useLocation } from "react-router-dom";
import { useCallback, useRef, useState } from "react";
import {
  Squares2X2Icon,
  ChartBarIcon,
  Cog6ToothIcon,
  ChevronDownIcon,
  FolderPlusIcon,
  PlusIcon,
} from "@heroicons/react/24/outline";
import AgentFlock from "./AgentFlock";
import { useProjects } from "../api/hooks";
import ProjectOnboardingWizard from "../pages/project/onboarding";
import { useProjectRoots } from "../pages/project/onboarding/useProjectRoots";
import { useProjectCreatedNavigation } from "../pages/project/onboarding/useProjectCreatedNavigation";
import { useListNav } from "./hotkeys/useListNav";
import { workspaceNavigation, workspaceHref } from "./projectNavigation";
import ProjectTree from "./ProjectTree";
import { useNavOrganization } from "./useNavOrganization";
import { useShellPreferences } from "./useShellPreferences";
import { linkClass } from "./railStyles";

export default function LeftRail() {
  const { data: projects } = useProjects();
  const location = useLocation();
  const { projectId, tab, isWorkspace, search } = workspaceNavigation(location);
  const navRef = useListNav<HTMLElement>({ axis: "vertical" });
  // The Projects disclosure is the user's roaming preference on the daemon.
  const { prefs, update: updatePreferences } = useShellPreferences();
  const projectsOpen = prefs.projects_section_open;
  const setProjectsOpen = useCallback(
    (open: boolean) => {
      void updatePreferences((current) => ({ ...current, projects_section_open: open }));
    },
    [updatePreferences],
  );
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const addProjectRef = useRef<HTMLButtonElement>(null);
  const newFolderRef = useRef<HTMLButtonElement>(null);
  const roots = useProjectRoots();
  const { organization, update } = useNavOrganization();
  // Design §4.6: refresh the rail, expand Projects, select and open the new project.
  const expandProjects = useCallback(() => setProjectsOpen(true), [setProjectsOpen]);
  const onProjectCreated = useProjectCreatedNavigation(expandProjects);
  return (
    <aside className="col-start-1 row-start-2 flex h-full w-64 shrink-0 lg:w-72 flex-col overflow-hidden border-r border-gray-800 bg-gray-900">
      <nav ref={navRef} className="dashboard-scrollbar flex-1 space-y-6 overflow-y-auto p-3">
        <div className="space-y-0.5">
          <Link to={workspaceHref(projectId, tab, search)} data-listnav="1"
            aria-current={isWorkspace ? "page" : undefined} className={linkClass(isWorkspace)}>
            <Squares2X2Icon className="h-4 w-4" />
            <span>Command Center</span>
          </Link>
          <section aria-label="Projects" className="pt-1">
            <div className="flex items-center gap-1">
              <button
                type="button"
                data-listnav="1"
                aria-expanded={projectsOpen}
                aria-controls="project-links"
                onClick={() => setProjectsOpen(!projectsOpen)}
                className="flex min-w-0 flex-1 items-center gap-2 rounded-lg px-3 py-2 text-xs font-medium uppercase tracking-wide text-gray-500 hover:bg-gray-800 hover:text-gray-300"
              >
                <ChevronDownIcon className={`h-4 w-4 transition-transform ${projectsOpen ? "" : "-rotate-90"}`} />
                <span>Projects</span>
              </button>
              {/* Keep project-management actions compact and separate from the disclosure. */}
              <button
                ref={newFolderRef}
                type="button"
                data-listnav="1"
                aria-label="New folder"
                title="New folder"
                onClick={() => {
                  setProjectsOpen(true);
                  setCreatingFolder(true);
                }}
                className="shrink-0 rounded-md p-1.5 text-gray-500 hover:bg-gray-800 hover:text-gray-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-indigo-400"
              >
                <FolderPlusIcon className="h-4 w-4" />
              </button>
              {/* Design §4.1: a separate control so opening the wizard never toggles the disclosure. */}
              <button
                ref={addProjectRef}
                type="button"
                aria-label="Add project"
                title="Add project"
                onClick={() => setWizardOpen(true)}
                className="shrink-0 rounded-md p-1.5 text-gray-500 hover:bg-gray-800 hover:text-gray-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-indigo-400"
              >
                <PlusIcon className="h-4 w-4" />
              </button>
            </div>
            {projectsOpen && (
              <ProjectTree
                projects={projects ?? []}
                organization={organization}
                update={update}
                creatingFolder={creatingFolder}
                onCloseFolderForm={() => {
                  setCreatingFolder(false);
                  newFolderRef.current?.focus();
                }}
                activeProjectId={projectId}
                tab={tab}
                search={search}
              />
            )}
          </section>
          <NavLink to="/metrics" data-listnav="1" className={({ isActive }) => linkClass(isActive)}>
            <ChartBarIcon className="h-4 w-4" />
            <span>Metrics</span>
          </NavLink>
          <NavLink to="/settings" data-listnav="1" className={({ isActive }) => linkClass(isActive)}>
            <Cog6ToothIcon className="h-4 w-4" />
            <span>Settings</span>
          </NavLink>
        </div>
        <AgentFlock />
      </nav>
      <ProjectOnboardingWizard
        open={wizardOpen}
        onClose={() => setWizardOpen(false)}
        returnFocusRef={addProjectRef}
        roots={roots}
        projectIds={(projects ?? []).map((project) => project.id)}
        onSuccess={onProjectCreated}
      />
    </aside>
  );
}
