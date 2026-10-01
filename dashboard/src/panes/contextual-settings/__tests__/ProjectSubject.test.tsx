import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import ProjectSubject from "../subjects/ProjectSubject";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const FALLBACK = { name: "Agent Queue", email: "agent-queue@localhost" };
const INSTALLATION = { name: "Ops Bot", email: "ops@example.com" };
const OVERRIDE = { name: "Jane Doe", email: "jane@example.com" };

const project = {
  id: "demo",
  name: "Demo",
  repo_url: "git@github.com:org/demo.git",
  repo_default_branch: "main",
  assignment_playbook_id: "default-assignment-routing",
  max_concurrent_agents: 2,
  credit_weight: 1,
  budget_limit: null,
  paused: false,
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

const unconfiguredProject = {
  ...project,
  git_identity: {
    ...FALLBACK,
    source: "fallback",
    configured: false,
    installation: null,
    project_override: null,
    fallback: FALLBACK,
  },
};

const overriddenProject = {
  ...project,
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

function mockProject(data: object) {
  vi.spyOn(hooks, "useProject").mockReturnValue({
    data,
    isLoading: false,
    error: null,
  } as unknown as ReturnType<typeof hooks.useProject>);
}

function mockEdit(mutateAsync = vi.fn().mockResolvedValue(project)) {
  vi.spyOn(hooks, "useEditProject").mockReturnValue({
    mutateAsync,
    isPending: false,
  } as unknown as ReturnType<typeof hooks.useEditProject>);
  return mutateAsync;
}

type Action = { id: string; disabled?: boolean; onClick: () => void };

function renderSubject() {
  const toolbar: { current: Action[] } = { current: [] };
  render(
    <ProjectSubject
      args={{ subject: "project", subjectId: "demo" }}
      close={vi.fn()}
      setArgs={vi.fn()}
      setToolbar={(actions) => {
        toolbar.current = actions as Action[];
      }}
      setShortcuts={vi.fn()}
    />,
    { wrapper },
  );
  return {
    action: (id: string) => toolbar.current.find((a) => a.id === id)!,
  };
}

describe("ProjectSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockProject(project);
  });

  it("renders repo_url read-only and enables Save once edited", async () => {
    const mutateAsync = vi.fn().mockResolvedValue(project);
    vi.spyOn(hooks, "useEditProject").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProject>);

    const setToolbar = vi.fn();
    render(
      <ProjectSubject
        args={{ subject: "project", subjectId: "demo" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={setToolbar}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByText("git@github.com:org/demo.git")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("git@github.com:org/demo.git")).not.toBeInTheDocument();

    const lastToolbarCall = () => setToolbar.mock.calls[setToolbar.mock.calls.length - 1]![0];
    expect(lastToolbarCall().find((a: { id: string }) => a.id === "save").disabled).toBe(true);
    expect(lastToolbarCall().map((a: { id: string }) => a.id)).toEqual(["save", "discard", "open-full"]);

    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), "Demo v2");

    await waitFor(() =>
      expect(lastToolbarCall().find((a: { id: string }) => a.id === "save").disabled).toBe(false),
    );
  });

  it("save payload matches Config.tsx's shape", async () => {
    const mutateAsync = vi.fn().mockResolvedValue(project);
    vi.spyOn(hooks, "useEditProject").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProject>);

    let toolbar: { id: string; onClick: () => void }[] = [];
    render(
      <ProjectSubject
        args={{ subject: "project", subjectId: "demo" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(actions) => {
          toolbar = actions;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), "Demo v2");
    await waitFor(() => expect(toolbar.find((a) => a.id === "save")).toBeDefined());
    toolbar.find((a) => a.id === "save")!.onClick();

    // An untouched identity sends no git_identity_* keys, so a save of other
    // fields never clobbers it.
    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        project_id: "demo",
        name: "Demo v2",
        repo_default_branch: "main",
        max_concurrent_agents: 2,
        credit_weight: 1,
        budget_limit: null,
      }),
    );
    expect(mutateAsync.mock.calls[0]![0]).not.toHaveProperty("git_identity_name");
    expect(mutateAsync.mock.calls[0]![0]).not.toHaveProperty("git_identity_email");
  });

  it("Discard changes reverts the form and re-disables Save", async () => {
    vi.spyOn(hooks, "useEditProject").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProject>);

    let toolbar: { id: string; disabled?: boolean; onClick: () => void }[] = [];
    render(
      <ProjectSubject
        args={{ subject: "project", subjectId: "demo" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(actions) => {
          toolbar = actions;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), "Demo v2");
    await waitFor(() => expect(toolbar.find((a) => a.id === "discard")!.disabled).toBe(false));

    toolbar.find((a) => a.id === "discard")!.onClick();

    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("Demo"));
    await waitFor(() => expect(toolbar.find((a) => a.id === "save")!.disabled).toBe(true));
  });

  describe("Git commit identity", () => {
    it("shows the inherited installation default with an override control", () => {
      mockEdit();
      renderSubject();

      expect(screen.getByTestId("git-identity-effective")).toHaveTextContent(
        "Ops Bot <ops@example.com>",
      );
      expect(screen.getByTestId("git-identity-source")).toHaveTextContent("Installation default");
      expect(screen.getByRole("button", { name: "Override for this project" })).toBeInTheDocument();
      expect(screen.queryByLabelText("Git commit name")).not.toBeInTheDocument();
      expect(screen.queryByRole("note")).not.toBeInTheDocument();
      expect(screen.getByText(/existing history is never rewritten/)).toBeInTheDocument();
    });

    it("marks the fallback as unset and points at the installation default", () => {
      mockProject(unconfiguredProject);
      mockEdit();
      renderSubject();

      expect(screen.getByTestId("git-identity-effective")).toHaveTextContent(
        "Agent Queue <agent-queue@localhost>",
      );
      expect(screen.getByTestId("git-identity-source")).toHaveTextContent(
        "Fallback — installation default not configured",
      );
      const note = screen.getByRole("note");
      expect(note).toHaveTextContent("git_identity");
      expect(note).toHaveTextContent("aq system config git-identity");
      expect(screen.getByRole("link", { name: "Settings › Config" })).toHaveAttribute(
        "href",
        "/settings/config",
      );
    });

    it("override saves both fields together", async () => {
      const mutateAsync = mockEdit();
      const ui = renderSubject();

      await userEvent.click(screen.getByRole("button", { name: "Override for this project" }));
      expect(screen.getByLabelText("Git commit name")).toHaveValue("Ops Bot");
      expect(screen.getByLabelText("Git commit email")).toHaveValue("ops@example.com");

      await userEvent.clear(screen.getByLabelText("Git commit name"));
      await userEvent.type(screen.getByLabelText("Git commit name"), "Jane Doe");
      await userEvent.clear(screen.getByLabelText("Git commit email"));
      await userEvent.type(screen.getByLabelText("Git commit email"), "jane@example.com");
      await waitFor(() => expect(ui.action("save").disabled).toBe(false));
      ui.action("save").onClick();

      await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
      expect(mutateAsync.mock.calls[0]![0]).toMatchObject({
        project_id: "demo",
        git_identity_name: "Jane Doe",
        git_identity_email: "jane@example.com",
      });
    });

    it("reset to installation default saves both fields as empty strings", async () => {
      mockProject(overriddenProject);
      const mutateAsync = mockEdit();
      const ui = renderSubject();

      expect(screen.getByTestId("git-identity-source")).toHaveTextContent("Project override");
      expect(screen.getByLabelText("Git commit name")).toHaveValue("Jane Doe");
      expect(ui.action("save").disabled).toBe(true);

      await userEvent.click(screen.getByRole("button", { name: "Reset to installation default" }));
      expect(screen.queryByLabelText("Git commit name")).not.toBeInTheDocument();
      expect(screen.getByTestId("git-identity-pending-reset")).toHaveTextContent(
        "Ops Bot <ops@example.com>",
      );
      await waitFor(() => expect(ui.action("save").disabled).toBe(false));
      ui.action("save").onClick();

      await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
      expect(mutateAsync.mock.calls[0]![0]).toMatchObject({
        git_identity_name: "",
        git_identity_email: "",
      });
    });

    it("rejects '<' in the name and a malformed email before saving", async () => {
      mockProject(overriddenProject);
      const mutateAsync = mockEdit();
      const ui = renderSubject();

      await userEvent.clear(screen.getByLabelText("Git commit name"));
      await userEvent.type(screen.getByLabelText("Git commit name"), "a<b");
      expect(await screen.findByText("Name must not contain '<' or '>'")).toBeInTheDocument();
      expect(screen.getByLabelText("Git commit name")).toHaveAttribute("aria-invalid", "true");
      await waitFor(() => expect(ui.action("save").disabled).toBe(true));

      await userEvent.clear(screen.getByLabelText("Git commit name"));
      await userEvent.type(screen.getByLabelText("Git commit name"), "Jane Doe");
      await userEvent.clear(screen.getByLabelText("Git commit email"));
      await userEvent.type(screen.getByLabelText("Git commit email"), "jane at example");
      expect(
        await screen.findByText("Email must be one address of the form name@domain"),
      ).toBeInTheDocument();
      await waitFor(() => expect(ui.action("save").disabled).toBe(true));

      ui.action("save").onClick();
      expect(mutateAsync).not.toHaveBeenCalled();
    });

    it("rejects a value spanning lines", async () => {
      mockProject(overriddenProject);
      mockEdit();
      const ui = renderSubject();

      // A text input drops typed newlines; a pasted line separator survives.
      fireEvent.change(screen.getByLabelText("Git commit name"), {
        target: { value: "Jane\u2028Doe" },
      });
      expect(
        await screen.findByText("Name must be a single line without control characters"),
      ).toBeInTheDocument();
      await waitFor(() => expect(ui.action("save").disabled).toBe(true));
    });

    it("shows a daemon refusal on the field it names", async () => {
      mockProject(overriddenProject);
      const mutateAsync = mockEdit(
        vi.fn().mockRejectedValue(
          new Error("API 422: email: must be one address of the form name@domain"),
        ),
      );
      const ui = renderSubject();

      await userEvent.clear(screen.getByLabelText("Git commit email"));
      await userEvent.type(screen.getByLabelText("Git commit email"), "jane@example.org");
      await waitFor(() => expect(ui.action("save").disabled).toBe(false));
      ui.action("save").onClick();

      await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
      expect(
        await screen.findByText("Email must be one address of the form name@domain"),
      ).toBeInTheDocument();
      expect(screen.getByLabelText("Git commit email")).toHaveAttribute("aria-invalid", "true");
    });
  });
});
