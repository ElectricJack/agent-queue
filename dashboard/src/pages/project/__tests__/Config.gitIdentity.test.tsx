import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ProjectConfig from "../Config";
import * as hooks from "../../../api/hooks";

const FALLBACK = { name: "Agent Queue", email: "agent-queue@localhost" };
const INSTALLATION = { name: "Ops Bot", email: "ops@example.com" };
const OVERRIDE = { name: "Jane Doe", email: "jane@example.com" };

const base = {
  id: "demo",
  name: "Demo",
  repo_url: "git@github.com:org/demo.git",
  repo_default_branch: "main",
  assignment_playbook_id: "default-assignment-routing",
  max_concurrent_agents: 2,
  credit_weight: 1,
  budget_limit: null,
  paused: false,
};

const inheriting = {
  ...base,
  git_identity_name: null,
  git_identity_email: null,
  git_identity: {
    ...INSTALLATION,
    source: "installation",
    configured: true,
    installation: INSTALLATION,
    project_override: null,
    fallback: FALLBACK,
  },
};

const unconfigured = {
  ...inheriting,
  git_identity: {
    ...FALLBACK,
    source: "fallback",
    configured: false,
    installation: null,
    project_override: null,
    fallback: FALLBACK,
  },
};

const overridden = {
  ...base,
  git_identity_name: OVERRIDE.name,
  git_identity_email: OVERRIDE.email,
  git_identity: {
    ...OVERRIDE,
    source: "project",
    configured: true,
    installation: INSTALLATION,
    project_override: OVERRIDE,
    fallback: FALLBACK,
  },
};

function setup(project: object) {
  const mutateAsync = vi.fn().mockResolvedValue(project);
  vi.spyOn(hooks, "useProject").mockReturnValue({
    data: project,
    isLoading: false,
  } as unknown as ReturnType<typeof hooks.useProject>);
  vi.spyOn(hooks, "useEditProject").mockReturnValue({
    mutateAsync,
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useEditProject>);
  const idle = { mutate: vi.fn(), isPending: false };
  vi.spyOn(hooks, "usePauseProject").mockReturnValue(
    idle as unknown as ReturnType<typeof hooks.usePauseProject>,
  );
  vi.spyOn(hooks, "useResumeProject").mockReturnValue(
    idle as unknown as ReturnType<typeof hooks.useResumeProject>,
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/projects/demo/config"]}>
        <Routes>
          <Route path="/projects/:projectId/config" element={<ProjectConfig />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return mutateAsync;
}

describe("Project Settings › Git commit identity", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows the inherited installation default", () => {
    setup(inheriting);
    expect(screen.getByRole("heading", { name: "Git commit identity" })).toBeInTheDocument();
    expect(screen.getByTestId("git-identity-effective")).toHaveTextContent(
      "Ops Bot <ops@example.com>",
    );
    expect(screen.getByTestId("git-identity-source")).toHaveTextContent("Installation default");
    expect(screen.getByText(/existing history is never rewritten/)).toBeInTheDocument();
  });

  it("shows the fallback as unset, with where to set the installation default", () => {
    setup(unconfigured);
    expect(screen.getByTestId("git-identity-source")).toHaveTextContent(
      "Fallback — installation default not configured",
    );
    expect(screen.getByRole("note")).toHaveTextContent("aq system config git-identity");
    expect(screen.getByRole("link", { name: "Settings › Config" })).toHaveAttribute(
      "href",
      "/settings/config",
    );
  });

  it("Override from the read-only view opens the editor and saves both fields", async () => {
    const mutateAsync = setup(unconfigured);
    await userEvent.click(screen.getByRole("button", { name: "Override for this project" }));

    // Prefilled with what commits use now (here the fallback).
    expect(screen.getByLabelText("Git commit name")).toHaveValue("Agent Queue");
    await userEvent.clear(screen.getByLabelText("Git commit name"));
    await userEvent.type(screen.getByLabelText("Git commit name"), "Jane Doe");
    await userEvent.clear(screen.getByLabelText("Git commit email"));
    await userEvent.type(screen.getByLabelText("Git commit email"), "jane@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0]![0]).toMatchObject({
      project_id: "demo",
      git_identity_name: "Jane Doe",
      git_identity_email: "jane@example.com",
    });
  });

  it("Reset to installation default saves both fields as empty strings", async () => {
    const mutateAsync = setup(overridden);
    expect(screen.getByTestId("git-identity-source")).toHaveTextContent("Project override");
    await userEvent.click(screen.getByRole("button", { name: "Reset to installation default" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0]![0]).toMatchObject({
      git_identity_name: "",
      git_identity_email: "",
    });
  });

  it("an edit that leaves the identity alone sends no identity fields", async () => {
    const mutateAsync = setup(overridden);
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0]![0]).not.toHaveProperty("git_identity_name");
    expect(mutateAsync.mock.calls[0]![0]).not.toHaveProperty("git_identity_email");
  });

  it("blocks Save while the override is invalid", async () => {
    const mutateAsync = setup(overridden);
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    await userEvent.clear(screen.getByLabelText("Git commit email"));
    await userEvent.type(screen.getByLabelText("Git commit email"), "jane<at>example.com");

    expect(screen.getByText("Email must not contain '<' or '>'")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("shows a daemon refusal inline on the named field", async () => {
    const mutateAsync = setup(overridden);
    mutateAsync.mockRejectedValueOnce(new Error("API 422: name: must not be empty"));
    await userEvent.click(screen.getByRole("button", { name: "Edit" }));
    await userEvent.clear(screen.getByLabelText("Git commit name"));
    await userEvent.type(screen.getByLabelText("Git commit name"), "Jane Q Doe");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Name must not be empty")).toBeInTheDocument();
    // Still editing, and the generic error banner is not used for it.
    expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument();
    expect(screen.queryByText(/API 422/)).not.toBeInTheDocument();
  });
});
