import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import { useCallback, useRef, useState, type MouseEvent } from "react";
import {
  Squares2X2Icon,
  ChartBarIcon,
  Cog6ToothIcon,
  ChevronDownIcon,
  FolderPlusIcon,
  PlusIcon,
  DocumentTextIcon,
  DevicePhoneMobileIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import AgentFlock from "./AgentFlock";
import { useProjects } from "../api/hooks";
import { useWaitingReviewCount } from "../api/reviews";
import ProjectOnboardingWizard from "../pages/project/onboarding";
import { useProjectRoots } from "../pages/project/onboarding/useProjectRoots";
import { useProjectCreatedNavigation } from "../pages/project/onboarding/useProjectCreatedNavigation";
import { useListNav } from "./hotkeys/useListNav";
import { workspaceNavigation, workspaceHref } from "./projectNavigation";
import ProjectTree from "./ProjectTree";
import { useNavOrganization } from "./useNavOrganization";
import { useShellPreferences } from "./useShellPreferences";
import { linkClass } from "./railStyles";
import { useFocusTrap } from "../hooks/useFocusTrap";

/**
 * The shell's navigation. A column beside the page at ≥768 px; below that
 * (`variant="drawer"`) a modal drawer over it that traps focus, closes on
 * Escape, the backdrop or its close button, and whose links replace the
 * history entry that opened it (mobile dashboard §4.1).
 */
export default function LeftRail({ variant = "column", onClose }: { variant?: "column" | "drawer"; onClose?: () => void }) {
  const { data: projects } = useProjects();
  const waitingReviewCount = useWaitingReviewCount();
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
  const drawer = variant === "drawer";
  const panel = useRef<HTMLElement>(null);
  const navigate = useNavigate();
  // The wizard traps focus and handles Escape itself while it is open.
  useFocusTrap(panel, drawer && !wizardOpen, { onEscape: onClose });

  // In the drawer a link replaces the entry that opened the drawer, so Back
  // from the destination returns to the page, not to an open menu.
  const replaceDrawerLinks = (event: MouseEvent<HTMLElement>) => {
    const anchor = (event.target as HTMLElement).closest("a[href]");
    if (!anchor || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const href = anchor.getAttribute("href") ?? "";
    if (!href.startsWith("/")) return;
    event.preventDefault(); // react-router's Link skips its own navigation then
    navigate(href, { replace: true });
  };

  const aside = (
    <aside
      ref={panel}
      tabIndex={drawer ? -1 : undefined}
      role={drawer ? "dialog" : undefined}
      aria-modal={drawer || undefined}
      aria-label={drawer ? "Navigation" : undefined}
      onClickCapture={drawer ? replaceDrawerLinks : undefined}
      className={drawer
        ? "fixed inset-y-0 left-0 z-50 flex w-72 max-w-[85vw] flex-col overflow-hidden border-r border-gray-800 bg-gray-900 pt-safe pb-safe"
        : "col-start-1 row-start-2 flex h-full w-64 shrink-0 lg:w-72 flex-col overflow-hidden border-r border-gray-800 bg-gray-900"}
    >
      {drawer && (
        <div className="flex shrink-0 items-center justify-between border-b border-gray-800 pl-3">
          <span className="text-sm font-semibold">Agent Q</span>
          <button type="button" data-primary-control aria-label="Close navigation" title="Close navigation"
            onClick={onClose}
            className="inline-flex items-center justify-center text-gray-400 hover:bg-gray-800">
            <XMarkIcon className="h-5 w-5" />
          </button>
        </div>
      )}
      <nav ref={navRef} className="dashboard-scrollbar flex-1 space-y-6 overflow-y-auto p-3">
        <div className="space-y-0.5">
          <Link to={workspaceHref(projectId, tab, search)} data-listnav="1" data-primary-control
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
          <NavLink to="/metrics" data-listnav="1" data-primary-control className={({ isActive }) => linkClass(isActive)}>
            <ChartBarIcon className="h-4 w-4" />
            <span>Metrics</span>
          </NavLink>
          <NavLink to="/reviews" data-listnav="1" data-primary-control className={({ isActive }) => linkClass(isActive)}>
            <DocumentTextIcon className="h-4 w-4" />
            <span>Reviews</span>
            {waitingReviewCount > 0 && (
              <span className="ml-auto rounded-full bg-indigo-500/20 px-1.5 py-0.5 text-xs text-indigo-200">
                {waitingReviewCount}
              </span>
            )}
          </NavLink>
          <NavLink to="/settings" data-listnav="1" data-primary-control className={({ isActive }) => linkClass(isActive)}>
            <Cog6ToothIcon className="h-4 w-4" />
            <span>Settings</span>
          </NavLink>
          <NavLink to="/focus" data-listnav="1" data-primary-control className={({ isActive }) => linkClass(isActive)}>
            <DevicePhoneMobileIcon className="h-4 w-4" />
            <span>Focus view</span>
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
  if (!drawer) return aside;
  return (
    <>
      <div aria-hidden="true" className="fixed inset-0 z-40 bg-black/60" onClick={onClose} />
      {aside}
    </>
  );
}
