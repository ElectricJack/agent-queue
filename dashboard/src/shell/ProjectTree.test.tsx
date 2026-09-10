import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import ProjectTree from "./ProjectTree";
import { useNavOrganization } from "./useNavOrganization";
import {
  FOLDER_DRAG_TYPE,
  PROJECT_DRAG_TYPE,
  storedOrganization,
  type NavProject,
} from "./navOrganization";

const PROJECTS: NavProject[] = [
  { id: "alpha", name: "Alpha" },
  { id: "beta", name: "Beta" },
  { id: "gamma", name: "Gamma" },
];

function Harness({ projects = PROJECTS }: { projects?: NavProject[] }) {
  const { organization, update } = useNavOrganization();
  const [creatingFolder, setCreatingFolder] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setCreatingFolder(true)}>New folder</button>
      <ProjectTree
        projects={projects}
        organization={organization}
        update={update}
        creatingFolder={creatingFolder}
        onCloseFolderForm={() => setCreatingFolder(false)}
        activeProjectId="alpha"
        tab="graph"
        search=""
      />
    </>
  );
}

function renderTree(projects?: NavProject[]) {
  return render(
    <MemoryRouter initialEntries={["/command-center"]}>
      <Harness projects={projects} />
    </MemoryRouter>,
  );
}

/** jsdom has no DataTransfer; the handlers only use types/getData/setData. */
function dataTransfer(entries: Record<string, string> = {}) {
  const data: Record<string, string> = { ...entries };
  return {
    get types() { return Object.keys(data); },
    getData: (type: string) => data[type] ?? "",
    setData: (type: string, value: string) => { data[type] = value; },
    effectAllowed: "none",
    dropEffect: "none",
  };
}

function row(container: HTMLElement, projectId: string): HTMLElement {
  const el = container.querySelector<HTMLElement>(`[data-project-row="${projectId}"]`);
  if (!el) throw new Error(`no row for ${projectId}`);
  return el;
}

function folderRow(container: HTMLElement, index: number): HTMLElement {
  const rows = container.querySelectorAll<HTMLElement>("[data-folder-row]");
  const el = rows[index];
  if (!el) throw new Error(`no folder row at ${index}`);
  return el;
}

/** Drag `from` and drop it on `to`, carrying `type`. */
function dragTo(from: HTMLElement, to: HTMLElement, type: string, id: string) {
  const dt = dataTransfer();
  fireEvent.dragStart(from, { dataTransfer: dt });
  expect(dt.getData(type)).toBe(id);
  fireEvent.dragOver(to, { dataTransfer: dt });
  fireEvent.drop(to, { dataTransfer: dt });
}

async function addFolder(name: string) {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "New folder" }));
  await user.type(screen.getByRole("textbox", { name: "Folder name" }), name);
  await user.click(screen.getByRole("button", { name: "Create folder" }));
}

function projectOrder(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll<HTMLElement>("[data-project-row]")).map(
    (el) => el.dataset.projectRow ?? "",
  );
}

beforeEach(() => window.localStorage.clear());
afterEach(() => { cleanup(); window.localStorage.clear(); });

describe("ProjectTree without folders", () => {
  it("renders every project as a link in API order", () => {
    const { container } = renderTree();
    expect(projectOrder(container)).toEqual(["alpha", "beta", "gamma"]);
    expect(screen.getByRole("link", { name: "Alpha" })).toHaveAttribute("href", "/projects/alpha/graph");
    expect(screen.getByRole("link", { name: "Alpha" })).toHaveAttribute("aria-current", "page");
  });

  it("shows the empty state when there are no projects", () => {
    renderTree([]);
    expect(screen.getByText("No projects yet")).toBeInTheDocument();
  });
});

describe("creating, renaming and deleting folders", () => {
  it("creates a folder from the inline form and persists it", async () => {
    renderTree();
    await addFolder("Clients");
    expect(screen.getByRole("button", { name: /^Clients/ })).toBeInTheDocument();
    expect(storedOrganization().folders.map((f) => f.name)).toEqual(["Clients"]);
  });

  it("cancels the inline form with Escape without creating anything", async () => {
    const user = userEvent.setup();
    renderTree();
    await user.click(screen.getByRole("button", { name: "New folder" }));
    await user.type(screen.getByRole("textbox", { name: "Folder name" }), "Nope{Escape}");
    expect(screen.queryByRole("textbox", { name: "Folder name" })).not.toBeInTheDocument();
    expect(storedOrganization().folders).toEqual([]);
  });

  it("renames a folder in place", async () => {
    const user = userEvent.setup();
    renderTree();
    await addFolder("Work");
    await user.click(screen.getByRole("button", { name: "Rename folder Work" }));
    const input = screen.getByRole("textbox", { name: "Rename folder Work" });
    await user.clear(input);
    await user.type(input, "Clients");
    await user.click(screen.getByRole("button", { name: "Save folder name" }));
    expect(storedOrganization().folders.map((f) => f.name)).toEqual(["Clients"]);
  });

  it("collapses and expands a folder", async () => {
    const user = userEvent.setup();
    const { container } = renderTree();
    await addFolder("Work");
    dragTo(row(container, "beta"), folderRow(container, 0), PROJECT_DRAG_TYPE, "beta");
    const toggle = screen.getByRole("button", { name: /^Work/ });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: /^Work/ })).toHaveAttribute("aria-expanded", "false");
    expect(projectOrder(container)).toEqual(["alpha", "gamma"]);
    expect(storedOrganization().folders[0]!.collapsed).toBe(true);
  });

  it("returns a deleted folder's projects to the root", async () => {
    const user = userEvent.setup();
    const { container } = renderTree();
    await addFolder("Work");
    dragTo(row(container, "beta"), folderRow(container, 0), PROJECT_DRAG_TYPE, "beta");
    await user.click(screen.getByRole("button", { name: "Delete folder Work" }));
    expect(screen.queryByRole("button", { name: /^Work/ })).not.toBeInTheDocument();
    expect(projectOrder(container)).toContain("beta");
    expect(storedOrganization().assignments).toEqual({});
  });
});

describe("dragging projects", () => {
  it("moves a project into a folder when dropped on its header", async () => {
    const { container } = renderTree();
    await addFolder("Work");
    dragTo(row(container, "gamma"), folderRow(container, 0), PROJECT_DRAG_TYPE, "gamma");
    const section = screen.getByRole("region", { name: "Folder Work" });
    expect(within(section).getByRole("link", { name: "Gamma" })).toBeInTheDocument();
    expect(storedOrganization().assignments).toEqual({ gamma: expect.any(String) });
  });

  it("reorders projects when dropped on another row", async () => {
    const { container } = renderTree();
    await addFolder("Work");
    dragTo(row(container, "gamma"), row(container, "alpha"), PROJECT_DRAG_TYPE, "gamma");
    expect(projectOrder(container)).toEqual(["gamma", "alpha", "beta"]);
  });

  it("moves a project back out through the root drop zone", async () => {
    const { container } = renderTree();
    await addFolder("Work");
    dragTo(row(container, "beta"), folderRow(container, 0), PROJECT_DRAG_TYPE, "beta");
    expect(storedOrganization().assignments.beta).toBeDefined();
    dragTo(row(container, "beta"), screen.getByTestId("rail-root-dropzone"), PROJECT_DRAG_TYPE, "beta");
    expect(storedOrganization().assignments).toEqual({});
  });

  it("expands a collapsed folder that receives a drop", async () => {
    const user = userEvent.setup();
    const { container } = renderTree();
    await addFolder("Work");
    await user.click(screen.getByRole("button", { name: /^Work/ }));
    expect(storedOrganization().folders[0]!.collapsed).toBe(true);
    dragTo(row(container, "beta"), folderRow(container, 0), PROJECT_DRAG_TYPE, "beta");
    expect(storedOrganization().folders[0]!.collapsed).toBe(false);
  });

  it("ignores a drop that carries no recognised payload", async () => {
    const { container } = renderTree();
    await addFolder("Work");
    const dt = dataTransfer({ "text/uri-list": "https://example.test" });
    fireEvent.drop(screen.getByTestId("rail-root-dropzone"), { dataTransfer: dt });
    expect(projectOrder(container)).toEqual(["alpha", "beta", "gamma"]);
    expect(storedOrganization().assignments).toEqual({});
  });

  it("only allows a drop for payloads the target accepts", async () => {
    const { container } = renderTree();
    await addFolder("Work");
    const project = dataTransfer({ [PROJECT_DRAG_TYPE]: "alpha" });
    const folder = dataTransfer({ [FOLDER_DRAG_TYPE]: "f1" });
    expect(fireEvent.dragOver(row(container, "beta"), { dataTransfer: project })).toBe(false);
    expect(fireEvent.dragOver(row(container, "beta"), { dataTransfer: folder })).toBe(true);
    expect(fireEvent.dragOver(folderRow(container, 0), { dataTransfer: folder })).toBe(false);
  });
});

describe("dragging folders", () => {
  it("reorders folders when one is dropped on another", async () => {
    const { container } = renderTree();
    await addFolder("A");
    await addFolder("B");
    const id = storedOrganization().folders[1]!.id;
    dragTo(folderRow(container, 1), folderRow(container, 0), FOLDER_DRAG_TYPE, id);
    expect(storedOrganization().folders.map((f) => f.name)).toEqual(["B", "A"]);
  });

  it("sends a folder to the end through the root drop zone", async () => {
    const { container } = renderTree();
    await addFolder("A");
    await addFolder("B");
    const id = storedOrganization().folders[0]!.id;
    dragTo(folderRow(container, 0), screen.getByTestId("rail-root-dropzone"), FOLDER_DRAG_TYPE, id);
    expect(storedOrganization().folders.map((f) => f.name)).toEqual(["B", "A"]);
  });
});

describe("keyboard equivalents", () => {
  it("moves a project between folders with the per-row select", async () => {
    const user = userEvent.setup();
    renderTree();
    await addFolder("Work");
    const select = screen.getByRole("combobox", { name: "Move Beta to folder" });
    await user.selectOptions(select, "Work");
    const section = screen.getByRole("region", { name: "Folder Work" });
    expect(within(section).getByRole("link", { name: "Beta" })).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Move Beta to folder" }), "No folder");
    expect(storedOrganization().assignments).toEqual({});
  });

  it("reorders folders with the up and down buttons", async () => {
    const user = userEvent.setup();
    renderTree();
    await addFolder("A");
    await addFolder("B");
    expect(screen.getByRole("button", { name: "Move folder A up" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Move folder B down" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Move folder A down" }));
    expect(storedOrganization().folders.map((f) => f.name)).toEqual(["B", "A"]);
    await user.click(screen.getByRole("button", { name: "Move folder A up" }));
    expect(storedOrganization().folders.map((f) => f.name)).toEqual(["A", "B"]);
  });
});
