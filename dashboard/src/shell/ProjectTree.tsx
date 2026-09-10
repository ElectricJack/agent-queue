import { useMemo, useState, type DragEvent } from "react";
import { Link } from "react-router-dom";
import {
  CheckIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  FolderIcon,
  PencilSquareIcon,
  TrashIcon,
  XMarkIcon,
} from "@heroicons/react/24/outline";
import {
  FOLDER_DRAG_TYPE,
  PROJECT_DRAG_TYPE,
  createFolder,
  deleteFolder,
  moveFolder,
  moveProject,
  navTree,
  nudgeFolder,
  renameFolder,
  toggleFolder,
  withProjectsRanked,
  type MoveProjectTarget,
  type NavOrganization,
  type NavProject,
} from "./navOrganization";
import { linkClass } from "./railStyles";
import { workspaceHref, type WorkspaceTab } from "./projectNavigation";

interface Props {
  projects: readonly NavProject[];
  organization: NavOrganization;
  update: (next: (org: NavOrganization) => NavOrganization) => void;
  creatingFolder: boolean;
  onCloseFolderForm: () => void;
  activeProjectId: string | null;
  tab: WorkspaceTab;
  search: string;
}

type Dragging = { kind: "project" | "folder"; id: string } | null;

function projectLabel(project: NavProject): string {
  return project.name ?? project.id;
}

function payload(event: DragEvent, type: string): string | null {
  const value = event.dataTransfer.getData(type) || event.dataTransfer.getData("text/plain");
  return value || null;
}

function carries(event: DragEvent, type: string): boolean {
  return Array.from(event.dataTransfer.types ?? []).includes(type);
}

/**
 * The Projects section of the left rail: folders, drag/drop, and a keyboard
 * equivalent for every drag gesture. Spec:
 * docs/superpowers/specs/2026-09-07-project-folders-and-drag-drop-design.md.
 */
export default function ProjectTree({
  projects, organization, update, creatingFolder, onCloseFolderForm, activeProjectId, tab, search,
}: Props) {
  const [dragging, setDragging] = useState<Dragging>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const tree = useMemo(() => navTree(projects, organization), [projects, organization]);

  const dropProject = (projectId: string, target: MoveProjectTarget) => {
    update((org) => moveProject(withProjectsRanked(org, projects), projectId, target));
  };

  const onProjectDragStart = (event: DragEvent, projectId: string) => {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData(PROJECT_DRAG_TYPE, projectId);
    event.dataTransfer.setData("text/plain", projectId);
    setDragging({ kind: "project", id: projectId });
  };

  const onFolderDragStart = (event: DragEvent, folderId: string) => {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData(FOLDER_DRAG_TYPE, folderId);
    event.dataTransfer.setData("text/plain", folderId);
    setDragging({ kind: "folder", id: folderId });
  };

  const allowDrop = (event: DragEvent, ...types: string[]) => {
    if (!types.some((type) => carries(event, type))) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
  };

  /** A row drop places the dragged project before that row, in that row's folder. */
  const onRowDrop = (event: DragEvent, folderId: string | null, beforeProjectId: string) => {
    if (!carries(event, PROJECT_DRAG_TYPE)) return;
    event.preventDefault();
    event.stopPropagation();
    const dragged = payload(event, PROJECT_DRAG_TYPE);
    setDragging(null);
    if (dragged && dragged !== beforeProjectId) dropProject(dragged, { folderId, beforeProjectId });
  };

  /** A folder header takes both kinds: a project joins it, a folder lands before it. */
  const onFolderDrop = (event: DragEvent, folderId: string) => {
    event.preventDefault();
    event.stopPropagation();
    const project = carries(event, PROJECT_DRAG_TYPE) ? payload(event, PROJECT_DRAG_TYPE) : null;
    const folder = carries(event, FOLDER_DRAG_TYPE) ? payload(event, FOLDER_DRAG_TYPE) : null;
    setDragging(null);
    if (project) {
      update((org) => toggleFolder(moveProject(withProjectsRanked(org, projects), project, { folderId }), folderId, false));
    } else if (folder && folder !== folderId) {
      update((org) => moveFolder(org, folder, folderId));
    }
  };

  const onRootDrop = (event: DragEvent) => {
    if (!carries(event, PROJECT_DRAG_TYPE) && !carries(event, FOLDER_DRAG_TYPE)) return;
    event.preventDefault();
    const project = carries(event, PROJECT_DRAG_TYPE) ? payload(event, PROJECT_DRAG_TYPE) : null;
    const folder = carries(event, FOLDER_DRAG_TYPE) ? payload(event, FOLDER_DRAG_TYPE) : null;
    setDragging(null);
    if (project) dropProject(project, { folderId: null });
    else if (folder) update((org) => moveFolder(org, folder, null));
  };

  const projectRow = (project: NavProject, folderId: string | null) => {
    const label = projectLabel(project);
    return (
      <div
        key={project.id}
        className="group/project flex items-center gap-1"
        draggable
        onDragStart={(event) => onProjectDragStart(event, project.id)}
        onDragEnd={() => setDragging(null)}
        onDragOver={(event) => allowDrop(event, PROJECT_DRAG_TYPE)}
        onDrop={(event) => onRowDrop(event, folderId, project.id)}
        data-project-row={project.id}
      >
        <Link
          to={workspaceHref(project.id, tab, search)}
          data-listnav="1"
          aria-current={activeProjectId === project.id ? "page" : undefined}
          className={`min-w-0 flex-1 ${linkClass(activeProjectId === project.id)}`}
        >
          <FolderIcon className="h-4 w-4 shrink-0" />
          <span className="truncate">{label}</span>
        </Link>
        {/* Design §5: the keyboard/screen-reader equivalent of dragging this row. */}
        <select
          aria-label={`Move ${label} to folder`}
          value={folderId ?? ""}
          onChange={(event) => dropProject(project.id, { folderId: event.target.value || null })}
          className="w-6 shrink-0 rounded border border-gray-700 bg-gray-800 text-xs text-gray-300 opacity-0 focus:w-auto focus:opacity-100 group-hover/project:w-auto group-hover/project:opacity-100"
        >
          <option value="">No folder</option>
          {tree.folders.map((folder) => (
            <option key={folder.id} value={folder.id}>{folder.name}</option>
          ))}
        </select>
      </div>
    );
  };

  return (
    <div id="project-links" className="space-y-0.5">
      {creatingFolder && (
        <form
          className="flex items-center gap-1 px-1 pb-1"
          onSubmit={(event) => {
            event.preventDefault();
            const input = new FormData(event.currentTarget).get("folderName");
            const name = typeof input === "string" ? input.trim() : "";
            if (name) update((org) => createFolder(org, name).org);
            onCloseFolderForm();
          }}
        >
          <input
            name="folderName"
            aria-label="Folder name"
            autoFocus
            placeholder="Folder name"
            onKeyDown={(event) => {
              if (event.key !== "Escape") return;
              event.preventDefault();
              onCloseFolderForm();
            }}
            className="min-w-0 flex-1 rounded border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-gray-100"
          />
          <button type="submit" aria-label="Create folder" className="rounded p-1 text-gray-400 hover:text-gray-100">
            <CheckIcon className="h-4 w-4" />
          </button>
          <button
            type="button"
            aria-label="Cancel new folder"
            onClick={onCloseFolderForm}
            className="rounded p-1 text-gray-400 hover:text-gray-100"
          >
            <XMarkIcon className="h-4 w-4" />
          </button>
        </form>
      )}

      {tree.folders.map((folder, index) => (
        <section key={folder.id} aria-label={`Folder ${folder.name}`}>
          <div
            className={`group/folder flex items-center gap-1 rounded-lg ${
              dragging ? "outline-dashed outline-1 outline-transparent hover:outline-indigo-500/60" : ""
            }`}
            draggable={editing !== folder.id}
            onDragStart={(event) => onFolderDragStart(event, folder.id)}
            onDragEnd={() => setDragging(null)}
            onDragOver={(event) => allowDrop(event, PROJECT_DRAG_TYPE, FOLDER_DRAG_TYPE)}
            onDrop={(event) => onFolderDrop(event, folder.id)}
            data-folder-row={folder.id}
          >
            {editing === folder.id ? (
              <form
                className="flex min-w-0 flex-1 items-center gap-1 px-1 py-1"
                onSubmit={(event) => {
                  event.preventDefault();
                  const value = new FormData(event.currentTarget).get("folderName");
                  if (typeof value === "string") update((org) => renameFolder(org, folder.id, value));
                  setEditing(null);
                }}
              >
                <input
                  name="folderName"
                  aria-label={`Rename folder ${folder.name}`}
                  defaultValue={folder.name}
                  autoFocus
                  onKeyDown={(event) => {
                    if (event.key !== "Escape") return;
                    event.preventDefault();
                    setEditing(null);
                  }}
                  className="min-w-0 flex-1 rounded border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-gray-100"
                />
                <button type="submit" aria-label="Save folder name" className="rounded p-1 text-gray-400 hover:text-gray-100">
                  <CheckIcon className="h-4 w-4" />
                </button>
              </form>
            ) : (
              <>
                <button
                  type="button"
                  data-listnav="1"
                  aria-expanded={!folder.collapsed}
                  aria-controls={`folder-contents-${folder.id}`}
                  onClick={() => update((org) => toggleFolder(org, folder.id))}
                  className="flex min-w-0 flex-1 items-center gap-2 rounded-lg px-3 py-2 text-sm text-gray-300 hover:bg-gray-800 hover:text-gray-100"
                >
                  <ChevronDownIcon className={`h-4 w-4 shrink-0 transition-transform ${folder.collapsed ? "-rotate-90" : ""}`} />
                  <span className="truncate">{folder.name}</span>
                  <span className="ml-auto shrink-0 text-xs text-gray-600">{folder.projects.length}</span>
                </button>
                {/* Kept in the DOM so the keyboard and screen readers reach them. */}
                <span className="flex shrink-0 items-center opacity-0 focus-within:opacity-100 group-hover/folder:opacity-100">
                  <button
                    type="button"
                    aria-label={`Move folder ${folder.name} up`}
                    disabled={index === 0}
                    onClick={() => update((org) => nudgeFolder(org, folder.id, -1))}
                    className="rounded p-1 text-gray-500 hover:text-gray-100 disabled:opacity-30"
                  >
                    <ChevronUpIcon className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    aria-label={`Move folder ${folder.name} down`}
                    disabled={index === tree.folders.length - 1}
                    onClick={() => update((org) => nudgeFolder(org, folder.id, 1))}
                    className="rounded p-1 text-gray-500 hover:text-gray-100 disabled:opacity-30"
                  >
                    <ChevronDownIcon className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    aria-label={`Rename folder ${folder.name}`}
                    onClick={() => setEditing(folder.id)}
                    className="rounded p-1 text-gray-500 hover:text-gray-100"
                  >
                    <PencilSquareIcon className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    aria-label={`Delete folder ${folder.name}`}
                    onClick={() => update((org) => deleteFolder(org, folder.id))}
                    className="rounded p-1 text-gray-500 hover:text-red-300"
                  >
                    <TrashIcon className="h-3.5 w-3.5" />
                  </button>
                </span>
              </>
            )}
          </div>
          {!folder.collapsed && (
            <div id={`folder-contents-${folder.id}`} className="ml-3 space-y-0.5 border-l border-gray-800 pl-2">
              {folder.projects.length === 0 ? (
                <p className="px-3 py-2 text-xs text-gray-600">Empty — drop a project here</p>
              ) : (
                folder.projects.map((project) => projectRow(project, folder.id))
              )}
            </div>
          )}
        </section>
      ))}

      {projects.length === 0 && <p className="px-3 py-2 text-xs text-gray-600">No projects yet</p>}
      {tree.loose.map((project) => projectRow(project, null))}

      {/* Drop here to leave a folder; only takes space while something is dragged. */}
      <div
        data-testid="rail-root-dropzone"
        onDragOver={(event) => allowDrop(event, PROJECT_DRAG_TYPE, FOLDER_DRAG_TYPE)}
        onDrop={onRootDrop}
        className={dragging ? "mx-1 rounded border border-dashed border-gray-700 px-3 py-2 text-xs text-gray-600" : "h-1"}
      >
        {dragging ? "Move out of every folder" : null}
      </div>
    </div>
  );
}
