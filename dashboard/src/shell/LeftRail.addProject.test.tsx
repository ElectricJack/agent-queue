import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import LeftRail from "./LeftRail";

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

afterEach(cleanup);

function renderRail() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/command-center"]}>
        <LeftRail />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function projectsToggle() {
  return screen.getByRole("button", { name: "Projects" });
}
function addButton() {
  return screen.getByRole("button", { name: "Add project" });
}

describe("LeftRail Add project button", () => {
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
