/**
 * Shared organization of the left rail's Projects section: named folders,
 * project-to-folder assignments, and a rail ordering. Design spec:
 * docs/superpowers/specs/2026-09-07-project-folders-and-drag-drop-design.md.
 *
 * Everything here is a pure function over the generated server value. The
 * persistence boundary lives in useNavOrganization, not in browser storage.
 */

import type {
  NavFolder as ApiNavFolder,
  NavOrganization as ApiNavOrganization,
} from "../api/client";

/** Private drag payloads, so a folder drag is never read as a project drop. */
export const PROJECT_DRAG_TYPE = "application/x-aq-nav-project";
export const FOLDER_DRAG_TYPE = "application/x-aq-nav-folder";

export type NavFolder = Omit<ApiNavFolder, "collapsed"> & { collapsed: boolean };
export type NavOrganization = Omit<ApiNavOrganization, "folders" | "assignments" | "project_order"> & {
  /** Folders in rail order. */
  folders: NavFolder[];
  /** projectId -> folderId. An entry for an unknown folder renders at the root. */
  assignments: Record<string, string>;
  /** One global ranking of project ids; each folder renders its own slice. */
  project_order: string[];
};

export interface NavProject {
  id: string;
  name?: string | null;
}

export interface NavFolderNode<P extends NavProject> extends NavFolder {
  projects: P[];
}

export interface NavTree<P extends NavProject> {
  folders: NavFolderNode<P>[];
  /** Projects that sit at the rail root, outside every folder. */
  loose: P[];
}

export const EMPTY_ORGANIZATION: NavOrganization = { folders: [], assignments: {}, project_order: [] };

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

/** Coerce anything at all into a usable organization; never throws. */
export function parseOrganization(raw: unknown): NavOrganization {
  if (!isRecord(raw)) return EMPTY_ORGANIZATION;
  const folders: NavFolder[] = [];
  const seenFolders = new Set<string>();
  if (Array.isArray(raw.folders)) {
    for (const entry of raw.folders) {
      if (!isRecord(entry)) continue;
      const { id, name, collapsed } = entry;
      if (typeof id !== "string" || !id || seenFolders.has(id)) continue;
      seenFolders.add(id);
      folders.push({
        id,
        name: typeof name === "string" && name.trim() ? name : "Folder",
        collapsed: collapsed === true,
      });
    }
  }
  const assignments: Record<string, string> = {};
  if (isRecord(raw.assignments)) {
    for (const [projectId, folderId] of Object.entries(raw.assignments)) {
      if (typeof folderId !== "string" || !seenFolders.has(folderId)) continue;
      assignments[projectId] = folderId;
    }
  }
  const project_order: string[] = [];
  const seenProjects = new Set<string>();
  if (Array.isArray(raw.project_order)) {
    for (const id of raw.project_order) {
      if (typeof id !== "string" || !id || seenProjects.has(id)) continue;
      seenProjects.add(id);
      project_order.push(id);
    }
  }
  return { folders, assignments, project_order };
}

/**
 * Project the stored organization onto the live project list. The organization
 * is advisory: unseen projects land at the root in API order, and ids that are
 * no longer projects are ignored rather than rendered or pruned.
 */
export function navTree<P extends NavProject>(
  projects: readonly P[],
  org: NavOrganization,
): NavTree<P> {
  const rank = new Map<string, number>();
  org.project_order.forEach((id, index) => {
    if (!rank.has(id)) rank.set(id, index);
  });
  const ranked = [...projects].sort((a, b) => {
    const left = rank.get(a.id);
    const right = rank.get(b.id);
    if (left === undefined && right === undefined) return 0; // stable: keep API order
    if (left === undefined) return 1;
    if (right === undefined) return -1;
    return left - right;
  });
  const folders: NavFolderNode<P>[] = org.folders.map((folder) => ({ ...folder, projects: [] }));
  const byId = new Map(folders.map((folder) => [folder.id, folder]));
  const loose: P[] = [];
  for (const project of ranked) {
    const folder = byId.get(org.assignments[project.id] ?? "");
    if (folder) folder.projects.push(project);
    else loose.push(project);
  }
  return { folders, loose };
}

let folderCounter = 0;

function newFolderId(): string {
  const crypto = typeof globalThis === "undefined" ? undefined : globalThis.crypto;
  if (crypto && typeof crypto.randomUUID === "function") return `f-${crypto.randomUUID()}`;
  folderCounter += 1;
  return `f-${Date.now().toString(36)}-${folderCounter}`;
}

export function createFolder(
  org: NavOrganization,
  name: string,
): { org: NavOrganization; folder: NavFolder } {
  const folder: NavFolder = { id: newFolderId(), name: name.trim() || "New folder", collapsed: false };
  return { org: { ...org, folders: [...org.folders, folder] }, folder };
}

export function renameFolder(org: NavOrganization, folderId: string, name: string): NavOrganization {
  const trimmed = name.trim();
  if (!trimmed) return org;
  return {
    ...org,
    folders: org.folders.map((folder) => (folder.id === folderId ? { ...folder, name: trimmed } : folder)),
  };
}

export function toggleFolder(org: NavOrganization, folderId: string, collapsed?: boolean): NavOrganization {
  return {
    ...org,
    folders: org.folders.map((folder) =>
      folder.id === folderId ? { ...folder, collapsed: collapsed ?? !folder.collapsed } : folder,
    ),
  };
}

/** Delete a folder; the projects inside fall back to the root, order intact. */
export function deleteFolder(org: NavOrganization, folderId: string): NavOrganization {
  const assignments: Record<string, string> = {};
  for (const [projectId, assigned] of Object.entries(org.assignments)) {
    if (assigned !== folderId) assignments[projectId] = assigned;
  }
  return { ...org, folders: org.folders.filter((folder) => folder.id !== folderId), assignments };
}

/** Place `folderId` before `beforeFolderId`, or last when that is null. */
export function moveFolder(
  org: NavOrganization,
  folderId: string,
  beforeFolderId: string | null,
): NavOrganization {
  if (folderId === beforeFolderId) return org;
  const moving = org.folders.find((folder) => folder.id === folderId);
  if (!moving) return org;
  const rest = org.folders.filter((folder) => folder.id !== folderId);
  const index = beforeFolderId === null ? -1 : rest.findIndex((folder) => folder.id === beforeFolderId);
  if (beforeFolderId !== null && index < 0) return org;
  const folders = [...rest];
  folders.splice(index < 0 ? folders.length : index, 0, moving);
  return { ...org, folders };
}

/** Shift a folder one slot up (-1) or down (+1) — the keyboard path for a folder drag. */
export function nudgeFolder(org: NavOrganization, folderId: string, delta: number): NavOrganization {
  const from = org.folders.findIndex((folder) => folder.id === folderId);
  if (from < 0) return org;
  const to = from + delta;
  if (to < 0 || to >= org.folders.length) return org;
  const folders = [...org.folders];
  const moving = folders.splice(from, 1);
  folders.splice(to, 0, ...moving);
  return { ...org, folders };
}

export interface MoveProjectTarget {
  /** null moves the project out to the rail root. */
  folderId: string | null;
  /** Place the project immediately before this one; omit/null to append. */
  beforeProjectId?: string | null;
}

export function moveProject(
  org: NavOrganization,
  projectId: string,
  target: MoveProjectTarget,
): NavOrganization {
  if (projectId === target.beforeProjectId) return org;
  if (target.folderId !== null && !org.folders.some((folder) => folder.id === target.folderId)) return org;
  const assignments = { ...org.assignments };
  if (target.folderId === null) delete assignments[projectId];
  else assignments[projectId] = target.folderId;

  const project_order = org.project_order.filter((id) => id !== projectId);
  const before = target.beforeProjectId ?? null;
  const index = before === null ? -1 : project_order.indexOf(before);
  project_order.splice(index < 0 ? project_order.length : index, 0, projectId);
  return { ...org, assignments, project_order };
}

/**
 * Seed `project_order` with the current rail order before the first reorder, so that
 * dropping one project does not reshuffle every project that storage has never
 * seen. Returns the organization unchanged when nothing new needs ranking.
 */
export function withProjectsRanked(
  org: NavOrganization,
  projects: readonly NavProject[],
): NavOrganization {
  const known = new Set(org.project_order);
  if (projects.every((project) => known.has(project.id))) return org;
  const tree = navTree(projects, org);
  const ranked = [...tree.folders.flatMap((folder) => folder.projects), ...tree.loose].map((p) => p.id);
  const seen = new Set(ranked);
  const trailing = org.project_order.filter((id) => !seen.has(id));
  return { ...org, project_order: [...ranked, ...trailing] };
}
