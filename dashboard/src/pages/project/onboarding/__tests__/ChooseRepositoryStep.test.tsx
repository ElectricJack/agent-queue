import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import ProjectOnboardingWizard from "..";
import { ChooseRepositoryStep } from "../ChooseRepositoryStep";
import { browseProjectRoot } from "../projectRootsClient";
import { createStepRegistry, type ProjectRootsSource } from "../index";

vi.mock("../projectRootsClient", () => ({ browseProjectRoot: vi.fn() }));

const browse = vi.mocked(browseProjectRoot);

const ROOTS: ProjectRootsSource = {
  status: "ready",
  roots: [
    { id: "read-only", label: "Read only", displayPath: "~/archive", readable: true, writable: false },
    { id: "dev", label: "Development", displayPath: "~/dev", readable: true, writable: true },
  ],
};

function Harness() {
  return (
    <MemoryRouter>
      <ProjectOnboardingWizard
        open
        onClose={() => {}}
        roots={ROOTS}
        steps={createStepRegistry({ repository: ChooseRepositoryStep })}
      />
    </MemoryRouter>
  );
}

async function goToRepository(user: ReturnType<typeof userEvent.setup>, source: RegExp) {
  await user.click(screen.getByRole("radio", { name: source }));
  await user.click(screen.getByRole("button", { name: "Next" }));
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ChooseRepositoryStep", () => {
  it("lists link roots, browses nested folders, returns through a breadcrumb, and selects a Git root", async () => {
    browse.mockImplementation(async (_rootId, relativePath) => {
      if (relativePath === "packages") {
        return {
          relativePath,
          entries: [{ name: "api", relativePath: "packages/api", isDirectory: true, isGitRepository: true, selectable: true }],
        };
      }
      return {
        relativePath: "",
        entries: [{ name: "packages", relativePath: "packages", isDirectory: true, isGitRepository: false, selectable: false }],
      };
    });
    const user = userEvent.setup();
    render(<Harness />);

    await goToRepository(user, /Existing local repository/);
    await user.selectOptions(screen.getByRole("combobox", { name: "Project root" }), "dev");
    await screen.findByRole("tree", { name: "Development directories" });
    await user.click(screen.getByRole("treeitem", { name: "packages" }));
    await screen.findByRole("treeitem", { name: /api/ });
    expect(screen.getByRole("navigation", { name: "Repository path" })).toHaveTextContent("packages");

    await user.click(screen.getByRole("button", { name: "Development" }));
    await screen.findByRole("treeitem", { name: "packages" });
    await user.click(screen.getByRole("treeitem", { name: "packages" }));
    const repository = await screen.findByRole("treeitem", { name: /api/ });
    await user.click(repository);

    expect(repository).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });

  it("does not select non-selectable directories and supports keyboard browser navigation", async () => {
    browse.mockResolvedValue({
      relativePath: "",
      entries: [
        { name: "notes", relativePath: "notes", isDirectory: true, isGitRepository: false, selectable: false },
        { name: "project", relativePath: "project", isDirectory: true, isGitRepository: true, selectable: true },
      ],
    });
    const user = userEvent.setup();
    render(<Harness />);

    await goToRepository(user, /Existing local repository/);
    await user.selectOptions(screen.getByRole("combobox", { name: "Project root" }), "dev");
    await screen.findByRole("tree", { name: "Development directories" });
    expect(screen.getByRole("treeitem", { name: "notes" })).toHaveAttribute("aria-selected", "false");
    await user.click(screen.getByRole("treeitem", { name: "notes" }));
    expect(screen.getByRole("treeitem", { name: "notes" })).toHaveAttribute("aria-selected", "false");
    await user.click(screen.getByRole("button", { name: "Development" }));
    await screen.findByRole("treeitem", { name: "notes" });

    const resetTree = screen.getByRole("tree", { name: "Development directories" });
    resetTree.focus();
    await user.keyboard("{ArrowDown}{Enter}");
    await waitFor(() => expect(screen.getByRole("treeitem", { name: /project/ })).toHaveAttribute("aria-selected", "true"));
    expect(resetTree).toHaveAttribute("aria-activedescendant", screen.getByRole("treeitem", { name: /project/ }).id);
  });

  it("limits new repositories to writable roots and validates the directory name immediately", async () => {
    const user = userEvent.setup();
    render(<Harness />);

    await goToRepository(user, /New repository/);
    const roots = screen.getByRole("combobox", { name: "Project root" });
    expect(within(roots).queryByRole("option", { name: /Read only/ })).not.toBeInTheDocument();
    await user.selectOptions(roots, "dev");
    const directory = screen.getByRole("textbox", { name: "New directory name" });
    await user.type(directory, "bad/name");
    expect(directory).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Enter a single directory name without path separators.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    await user.clear(directory);
    await user.type(directory, "new-project");
    expect(directory).not.toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
  });
});
