import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import LeftRail from "./LeftRail";
import { createFakeDashboardStateServer, TestDashboardState } from "../testUtils/dashboardState";

vi.mock("../api/hooks", () => ({
  useProjects: () => ({ data: [{ id: "p1", name: "Project one" }] }),
}));
vi.mock("./AgentFlock", () => ({ default: () => null }));
vi.mock("../pages/project/onboarding/useProjectRoots", () => ({
  useProjectRoots: () => ({
    status: "ready",
    roots: [{ id: "dev", label: "Development", displayPath: "~/dev", readable: true, writable: true }],
  }),
}));

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

function renderRail() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TestDashboardState server={createFakeDashboardStateServer()}>
        <MemoryRouter initialEntries={["/command-center"]}>
          <LeftRail />
        </MemoryRouter>
      </TestDashboardState>
    </QueryClientProvider>,
  );
}

function projectsToggle() {
  return screen.getByRole("button", { name: "Projects" });
}
function addButton() {
  return screen.getByRole("button", { name: "Add project" });
}
function newFolderButton() {
  return screen.getByRole("button", { name: "New folder" });
}

describe("LeftRail Add project button", () => {
  it("places a compact, labelled New folder control immediately before Add project", () => {
    renderRail();
    const newFolder = newFolderButton();
    const addProject = addButton();
    expect(newFolder).toHaveAttribute("title", "New folder");
    expect(newFolder).toHaveClass("p-1.5");
    expect(addProject.previousElementSibling).toBe(newFolder);
    expect(screen.queryByRole("button", { name: "New Project" })).not.toBeInTheDocument();
    expect(screen.queryByText("New folder")).not.toBeInTheDocument();
  });

  it("wires the icon to the existing new-folder form and restores focus after creation", async () => {
    const user = userEvent.setup();
    renderRail();
    await user.click(newFolderButton());
    expect(screen.getByRole("textbox", { name: "Folder name" })).toHaveFocus();
    await user.type(screen.getByRole("textbox", { name: "Folder name" }), "Clients");
    await user.click(screen.getByRole("button", { name: "Create folder" }));
    expect(screen.getByRole("button", { name: /^Clients/ })).toBeInTheDocument();
    expect(newFolderButton()).toHaveFocus();
  });

  it("renders a separate labelled button with a tooltip", () => {
    renderRail();
    const btn = addButton();
    expect(btn).toHaveAttribute("title", "Add project");
    expect(btn).not.toBe(projectsToggle());
    expect(projectsToggle()).not.toContainElement(btn);
  });

  it("opens the wizard on click without toggling the Projects disclosure", async () => {
    const user = userEvent.setup();
    renderRail();
    expect(projectsToggle()).toHaveAttribute("aria-expanded", "true");
    await user.click(addButton());
    expect(screen.getByRole("dialog", { name: "Add project" })).toBeInTheDocument();
    expect(projectsToggle()).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("link", { name: "Project one" })).toBeInTheDocument();
  });

  it("keeps the disclosure toggle working independently", async () => {
    const user = userEvent.setup();
    renderRail();
    await user.click(projectsToggle());
    expect(projectsToggle()).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await user.click(addButton());
    expect(screen.getByRole("dialog", { name: "Add project" })).toBeInTheDocument();
    expect(projectsToggle()).toHaveAttribute("aria-expanded", "false");
  });

  it("opens from the keyboard and returns focus to the button on close", async () => {
    const user = userEvent.setup();
    renderRail();
    addButton().focus();
    await user.keyboard("{Enter}");
    const dlg = screen.getByRole("dialog", { name: "Add project" });
    expect(dlg.contains(document.activeElement)).toBe(true);
    await user.click(within(dlg).getByRole("button", { name: "Close dialog" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(addButton()).toHaveFocus();
    await user.keyboard(" ");
    expect(screen.getByRole("dialog", { name: "Add project" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(addButton()).toHaveFocus();
  });
});
