import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
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

const project = {
  id: "demo",
  name: "Demo",
  repo_url: "git@github.com:org/demo.git",
  repo_default_branch: "main",
  default_profile_id: "",
  max_concurrent_agents: 2,
  credit_weight: 1,
  budget_limit: null,
  discord_channel_id: "",
  paused: false,
};

describe("ProjectSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "useProject").mockReturnValue({
      data: project,
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useProject>);
    vi.spyOn(hooks, "useProfiles").mockReturnValue({
      data: [],
    } as unknown as ReturnType<typeof hooks.useProfiles>);
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

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        project_id: "demo",
        name: "Demo v2",
        repo_default_branch: "main",
        default_profile_id: null,
        max_concurrent_agents: 2,
        credit_weight: 1,
        budget_limit: null,
        discord_channel_id: null,
      }),
    );
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
});
