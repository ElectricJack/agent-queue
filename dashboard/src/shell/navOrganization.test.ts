import { describe, expect, it } from "vitest";
import {
  EMPTY_ORGANIZATION,
  createFolder,
  deleteFolder,
  moveFolder,
  moveProject,
  navTree,
  nudgeFolder,
  parseOrganization,
  renameFolder,
  toggleFolder,
  withProjectsRanked,
  type NavOrganization,
} from "./navOrganization";

const projects = [
  { id: "alpha", name: "Alpha" },
  { id: "beta", name: "Beta" },
  { id: "gamma", name: "Gamma" },
];

function withFolders(...names: string[]): { org: NavOrganization; ids: string[] } {
  let org = EMPTY_ORGANIZATION;
  const ids: string[] = [];
  for (const name of names) {
    const created = createFolder(org, name);
    org = created.org;
    ids.push(created.folder.id);
  }
  return { org, ids };
}

describe("navTree", () => {
  it("renders the API order at the root when nothing is organized", () => {
    const tree = navTree(projects, EMPTY_ORGANIZATION);
    expect(tree.folders).toEqual([]);
    expect(tree.loose.map((p) => p.id)).toEqual(["alpha", "beta", "gamma"]);
  });

  it("buckets projects into their folders and honours the stored ranking", () => {
    const { org, ids } = withFolders("Work");
    const organized = moveProject(
      { ...org, project_order: ["gamma", "beta", "alpha"] },
      "beta",
      { folderId: ids[0]! },
    );
    const tree = navTree(projects, organized);
    expect(tree.folders[0]!.projects.map((p) => p.id)).toEqual(["beta"]);
    expect(tree.loose.map((p) => p.id)).toEqual(["gamma", "alpha"]);
  });

  it("sorts projects the organization has never seen after the ranked ones, in API order", () => {
    const tree = navTree(projects, { ...EMPTY_ORGANIZATION, project_order: ["gamma"] });
    expect(tree.loose.map((p) => p.id)).toEqual(["gamma", "alpha", "beta"]);
  });

  it("ignores ranked and assigned ids that are no longer projects", () => {
    const { org, ids } = withFolders("Work");
    const organized = moveProject({ ...org, project_order: ["ghost"] }, "ghost", { folderId: ids[0]! });
    const tree = navTree(projects, organized);
    expect(tree.folders[0]!.projects).toEqual([]);
    expect(tree.loose.map((p) => p.id)).toEqual(["alpha", "beta", "gamma"]);
    expect(organized.project_order).toContain("ghost"); // kept: the project may come back
  });

  it("renders a project assigned to a folder that no longer exists at the root", () => {
    const { org, ids } = withFolders("Work");
    const assigned = moveProject(org, "alpha", { folderId: ids[0]! });
    const tree = navTree(projects, deleteFolder(assigned, ids[0]!));
    expect(tree.folders).toEqual([]);
    expect(tree.loose.map((p) => p.id)).toEqual(["alpha", "beta", "gamma"]);
  });
});

describe("folder operations", () => {
  it("creates folders with distinct ids and falls back to a default name", () => {
    const { org, ids } = withFolders("Work", "   ");
    expect(new Set(ids).size).toBe(2);
    expect(org.folders.map((f) => f.name)).toEqual(["Work", "New folder"]);
  });

  it("renames a folder and ignores a blank name", () => {
    const { org, ids } = withFolders("Work");
    expect(renameFolder(org, ids[0]!, " Clients ").folders[0]!.name).toBe("Clients");
    expect(renameFolder(org, ids[0]!, "   ")).toBe(org);
  });

  it("toggles collapse, explicitly or by flipping", () => {
    const { org, ids } = withFolders("Work");
    expect(toggleFolder(org, ids[0]!).folders[0]!.collapsed).toBe(true);
    expect(toggleFolder(toggleFolder(org, ids[0]!), ids[0]!, false).folders[0]!.collapsed).toBe(false);
  });

  it("drops a folder's projects back to the root, keeping their order", () => {
    const { org, ids } = withFolders("Work");
    let organized = withProjectsRanked(org, projects);
    organized = moveProject(organized, "gamma", { folderId: ids[0]! });
    organized = moveProject(organized, "alpha", { folderId: ids[0]! });
    const before = navTree(projects, organized).folders[0]!.projects.map((p) => p.id);
    const tree = navTree(projects, deleteFolder(organized, ids[0]!));
    expect(tree.loose.map((p) => p.id).filter((id) => before.includes(id))).toEqual(before);
  });

  it("reorders folders by drop target and by keyboard nudge", () => {
    const { org, ids } = withFolders("A", "B", "C");
    expect(moveFolder(org, ids[2]!, ids[0]!).folders.map((f) => f.name)).toEqual(["C", "A", "B"]);
    expect(moveFolder(org, ids[0]!, null).folders.map((f) => f.name)).toEqual(["B", "C", "A"]);
    expect(moveFolder(org, ids[0]!, "missing")).toBe(org);
    expect(nudgeFolder(org, ids[1]!, -1).folders.map((f) => f.name)).toEqual(["B", "A", "C"]);
    expect(nudgeFolder(org, ids[2]!, 1)).toEqual(org);
    expect(nudgeFolder(org, ids[0]!, -1)).toEqual(org);
  });
});

describe("moveProject", () => {
  it("places a project before another one inside a folder", () => {
    const { org, ids } = withFolders("Work");
    let organized = withProjectsRanked(org, projects);
    organized = moveProject(organized, "alpha", { folderId: ids[0]! });
    organized = moveProject(organized, "gamma", { folderId: ids[0]!, beforeProjectId: "alpha" });
    expect(navTree(projects, organized).folders[0]!.projects.map((p) => p.id)).toEqual(["gamma", "alpha"]);
  });

  it("moves a project back out to the root", () => {
    const { org, ids } = withFolders("Work");
    const inFolder = moveProject(org, "alpha", { folderId: ids[0]! });
    const out = moveProject(inFolder, "alpha", { folderId: null });
    expect(out.assignments).toEqual({});
    expect(navTree(projects, out).loose.map((p) => p.id)).toContain("alpha");
  });

  it("refuses a drop onto itself or into an unknown folder", () => {
    const { org } = withFolders("Work");
    expect(moveProject(org, "alpha", { folderId: null, beforeProjectId: "alpha" })).toBe(org);
    expect(moveProject(org, "alpha", { folderId: "nope" })).toBe(org);
  });
});

describe("withProjectsRanked", () => {
  it("seeds the ranking from the rendered order and is then a no-op", () => {
    const seeded = withProjectsRanked(EMPTY_ORGANIZATION, projects);
    expect(seeded.project_order).toEqual(["alpha", "beta", "gamma"]);
    expect(withProjectsRanked(seeded, projects)).toBe(seeded);
  });

  it("keeps ids for projects that are not currently listed", () => {
    const seeded = withProjectsRanked({ ...EMPTY_ORGANIZATION, project_order: ["ghost"] }, projects);
    expect(seeded.project_order).toEqual(["alpha", "beta", "gamma", "ghost"]);
  });
});

describe("server value parsing", () => {
  it("drops junk entries rather than the whole organization", () => {
    const parsed = parseOrganization({
      folders: [
        { id: "f1", name: "Work" },
        { id: "f1", name: "Duplicate" },
        { name: "No id" },
        { id: "f2", name: "  ", collapsed: "yes" },
        7,
      ],
      assignments: { alpha: "f1", beta: "gone", gamma: 5 },
      project_order: ["alpha", "alpha", 9, "", "beta"],
    });
    expect(parsed.folders).toEqual([
      { id: "f1", name: "Work", collapsed: false },
      { id: "f2", name: "Folder", collapsed: false },
    ]);
    expect(parsed.assignments).toEqual({ alpha: "f1" });
    expect(parsed.project_order).toEqual(["alpha", "beta"]);
  });

  it("does not treat the retired local order field as a server value", () => {
    expect(parseOrganization({ order: ["alpha"] })).toEqual(EMPTY_ORGANIZATION);
  });
});
