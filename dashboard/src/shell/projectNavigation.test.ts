import { describe, expect, it } from "vitest";
import { projectNavigation, workspaceHref } from "./projectNavigation";

describe("project workspace destinations", () => {
  it.each(["graph", "tasks", "overview", "sessions", "workspaces", "profiles", "playbooks", "config"])(
    "retains the current %s tab and exact filter query when switching projects", (tab) => {
      const current = projectNavigation(`/projects/first/${tab}`);
      expect(current.isWorkspace).toBe(true);
      expect(workspaceHref("second", current.tab, "?q=a%20b&status=READY&completed=1"))
        .toBe(`/projects/second/${tab}?q=a%20b&status=READY&completed=1`);
    },
  );

  it("allows All projects on Tasks but requires a project for resources", () => {
    expect(workspaceHref(null, "tasks", "?q=x")).toBe("/command-center/tasks?q=x");
    expect(workspaceHref(null, "config", "?q=x")).toBe("/command-center/graph?q=x");
  });

  it("recognizes project and global index URLs without confusing other pages", () => {
    expect(projectNavigation("/projects/p1")).toEqual({ projectId: "p1", tab: "graph", isWorkspace: true });
    expect(projectNavigation("/command-center")).toEqual({ projectId: null, tab: "graph", isWorkspace: true });
    expect(projectNavigation("/agents")).toEqual({ projectId: null, tab: "graph", isWorkspace: false });
    expect(projectNavigation("/settings/config").isWorkspace).toBe(false);
  });

  it("encodes project IDs as a single URL segment and keeps them selected on return", () => {
    const href = workspaceHref("name with space", "graph");
    expect(href).toBe("/projects/name%20with%20space/graph");
    expect(projectNavigation(href).projectId).toBe("name with space");
  });
});
