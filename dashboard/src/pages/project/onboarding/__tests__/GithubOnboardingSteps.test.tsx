import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import ProjectOnboardingWizard, { createStepRegistry, useWizard, type ProjectRootsSource } from "..";
import { githubAuthStatus, githubOwners, searchGithub } from "../githubClient";
import { githubRepositoryDisplay } from "../githubUrl";

vi.mock("../githubClient", () => ({
  githubAuthStatus: vi.fn(),
  githubOwners: vi.fn(),
  searchGithub: vi.fn(),
}));

const authStatus = vi.mocked(githubAuthStatus);
const owners = vi.mocked(githubOwners);
const search = vi.mocked(searchGithub);

const ROOTS: ProjectRootsSource = {
  status: "ready",
  roots: [
    { id: "readonly", label: "Archive", displayPath: "~/archive", readable: true, writable: false },
    { id: "dev", label: "Development", displayPath: "~/dev", readable: true, writable: true },
  ],
};

function ReviewState() {
  const { state } = useWizard();
  return <pre data-testid="review-state">{JSON.stringify(state.source)}</pre>;
}

function Harness() {
  return <MemoryRouter><ProjectOnboardingWizard open onClose={() => {}} roots={ROOTS} steps={createStepRegistry({ review: ReviewState })} /></MemoryRouter>;
}

async function selectSource(user: ReturnType<typeof userEvent.setup>, label: RegExp) {
  await user.click(screen.getByRole("radio", { name: label }));
  await user.click(screen.getByRole("button", { name: "Next" }));
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("GitHub onboarding wizard panels", () => {
  it("searches paged repositories, selects a result, and carries its destination to review", async () => {
    authStatus.mockResolvedValue({ installed: true, authenticated: true });
    search.mockResolvedValue({
      repositories: [{ owner: "acme", name: "widgets", clone_url_https: "https://github.com/acme/widgets.git", default_branch: "trunk", visibility: "private", full_name: "acme/widgets" }],
      nextCursor: null,
    });
    const user = userEvent.setup();
    render(<Harness />);
    await selectSource(user, /Clone from GitHub/);
    const searchInput = screen.getByRole("textbox", { name: "Search GitHub repositories" });
    await user.type(searchInput, "widgets");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await user.click(await screen.findByRole("button", { name: /acme\/widgets/ }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Destination root" }), "dev");
    expect(screen.getByRole("textbox", { name: "Destination directory" })).toHaveValue("widgets");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByTestId("review-state")).toHaveTextContent('"githubRepository":{"owner":"acme","name":"widgets"');
    expect(screen.getByTestId("review-state")).toHaveTextContent('"rootId":"dev"');
    expect(search).toHaveBeenCalledWith("widgets", null);
  });

  it("accepts HTTPS, SSH, and shorthand paste forms while preserving raw text for validation", async () => {
    authStatus.mockResolvedValue({ installed: true, authenticated: true });
    const user = userEvent.setup();
    render(<Harness />);
    await selectSource(user, /Clone from GitHub/);
    const url = screen.getByRole("textbox", { name: "Or paste a GitHub repository URL" });
    await user.type(url, "git@github.com:acme/widgets.git");
    expect(screen.getByText("Will use GitHub repository acme/widgets. The original URL will be validated by the server.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Destination directory" })).toHaveValue("widgets");
    await user.clear(url);
    await user.type(url, "acme/another-project");
    expect(screen.getByText("Will use GitHub repository acme/another-project. The original URL will be validated by the server.")).toBeInTheDocument();
    await user.selectOptions(screen.getByRole("combobox", { name: "Destination root" }), "dev");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByTestId("review-state")).toHaveTextContent('"githubUrl":"acme/another-project"');
  });

  it("normalizes supported pasted forms only for display", () => {
    expect(githubRepositoryDisplay("https://github.com/acme/widgets.git")).toEqual({ owner: "acme", name: "widgets" });
    expect(githubRepositoryDisplay("git@github.com:acme/widgets.git")).toEqual({ owner: "acme", name: "widgets" });
    expect(githubRepositoryDisplay("acme/widgets")).toEqual({ owner: "acme", name: "widgets" });
  });

  it("shows setup guidance when GitHub CLI authentication is unavailable without blocking URL paste", async () => {
    authStatus.mockResolvedValue({ installed: true, authenticated: false, message: "Authentication required" });
    const user = userEvent.setup();
    render(<Harness />);
    await selectSource(user, /Clone from GitHub/);
    expect(await screen.findByText("Authentication required")).toHaveAttribute("role", "status");
    await user.type(screen.getByRole("textbox", { name: "Or paste a GitHub repository URL" }), "https://github.com/acme/widgets");
    expect(screen.getByText(/Will use GitHub repository acme\/widgets/)).toBeInTheDocument();
  });

  it("defaults init options safely and carries owner, name, and visibility to review", async () => {
    authStatus.mockResolvedValue({ installed: true, authenticated: true });
    owners.mockResolvedValue([{ login: "acme", name: "Acme" }]);
    const user = userEvent.setup();
    render(<Harness />);
    await selectSource(user, /New repository/);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    const readme = screen.getByRole("checkbox", { name: "Create initial README and commit" });
    const createGithub = screen.getByRole("checkbox", { name: "Create GitHub repository" });
    expect(readme).toBeChecked();
    expect(createGithub).not.toBeChecked();
    await user.click(createGithub);
    await screen.findByRole("option", { name: "Acme (acme)" });
    const repo = screen.getByRole("textbox", { name: "GitHub repository name" });
    await user.type(repo, "widgets");
    expect(screen.getByRole("radio", { name: "Private" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Public" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    const review = screen.getByTestId("review-state");
    expect(review).toHaveTextContent('"githubOwner":"acme"');
    expect(review).toHaveTextContent('"githubRepo":"widgets"');
    expect(review).toHaveTextContent('"githubVisibility":"public"');
  });
});
