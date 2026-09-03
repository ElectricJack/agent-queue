import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import ProfileSubject from "../subjects/ProfileSubject";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const profile = {
  id: "reviewer",
  name: "Reviewer",
  description: "Reviews PRs",
  harness: "codex",
  default_class: "standard-medium",
  permission_mode: "acceptEdits",
  codex_full_auto: false,
  claude_dangerously_skip_permissions: false,
  system_prompt_suffix: "Be terse.",
  allowed_tools: ["Read"],
  mcp_servers: [],
};

describe("ProfileSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "useGetProfile").mockReturnValue({
      data: profile,
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useGetProfile>);
    vi.spyOn(hooks, "useIntelligenceClasses").mockReturnValue({
      data: { success: true, classes: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useIntelligenceClasses>);
    vi.spyOn(hooks, "useMcpServers").mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useMcpServers>);
    vi.spyOn(hooks, "useToolCatalog").mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useToolCatalog>);
  });

  it("renders every drawer section", () => {
    vi.spyOn(hooks, "useEditProfile").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProfile>);

    render(
      <ProfileSubject
        args={{ subject: "profile", subjectId: "reviewer" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByText("Basics")).toBeInTheDocument();
    expect(screen.getByText("Intelligence class & permissions")).toBeInTheDocument();
    expect(screen.getByText("System prompt suffix")).toBeInTheDocument();
    expect(screen.getByText("MCP servers")).toBeInTheDocument();
    expect(screen.getByText("Allowed tools")).toBeInTheDocument();
    expect(screen.getByLabelText("Codex full auto")).toBeInTheDocument();
    expect(screen.queryByLabelText("Claude dangerously skip permissions")).not.toBeInTheDocument();
  });

  it("shows the dangerous Claude opt-in only for Claude profiles", () => {
    vi.spyOn(hooks, "useGetProfile").mockReturnValue({
      data: { ...profile, harness: "claude" },
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useGetProfile>);
    vi.spyOn(hooks, "useEditProfile").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProfile>);

    render(
      <ProfileSubject
        args={{ subject: "profile", subjectId: "reviewer" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByLabelText("Claude dangerously skip permissions")).toBeInTheDocument();
    expect(screen.queryByLabelText("Codex full auto")).not.toBeInTheDocument();
    expect(screen.getByText(/disables all permission prompts/i)).toBeInTheDocument();
  });

  it("marks legacy Codex bypass as stronger than sandboxed full auto", () => {
    vi.spyOn(hooks, "useGetProfile").mockReturnValue({
      data: { ...profile, permission_mode: "bypassPermissions" },
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useGetProfile>);
    vi.spyOn(hooks, "useEditProfile").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProfile>);

    render(
      <ProfileSubject
        args={{ subject: "profile", subjectId: "reviewer" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByLabelText("Codex full auto")).toBeDisabled();
    expect(screen.getByText(/disables approvals and sandbox restrictions/i)).toBeInTheDocument();
  });

  it("save payload matches SystemProfileEditDrawer's onSave shape", async () => {
    const mutateAsync = vi.fn().mockResolvedValue(profile);
    vi.spyOn(hooks, "useEditProfile").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProfile>);

    let toolbar: { id: string; onClick: () => void }[] = [];
    render(
      <ProfileSubject
        args={{ subject: "profile", subjectId: "reviewer" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(a) => {
          toolbar = a;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    await userEvent.click(screen.getByLabelText("Codex full auto"));
    await waitFor(() => expect(toolbar.find((a) => a.id === "save")).toBeDefined());
    toolbar.find((a) => a.id === "save")!.onClick();

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({
          profile_id: "reviewer",
          name: "Reviewer",
          default_class: "standard-medium",
          codex_full_auto: true,
          claude_dangerously_skip_permissions: false,
          mcp_servers: [],
        }),
      ),
    );
  });

  it("can disable a legacy Claude bypass through the canonical opt-in", async () => {
    vi.spyOn(hooks, "useGetProfile").mockReturnValue({
      data: {
        ...profile,
        harness: "claude",
        permission_mode: "bypassPermissions",
      },
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.useGetProfile>);
    const mutateAsync = vi.fn().mockResolvedValue(profile);
    vi.spyOn(hooks, "useEditProfile").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProfile>);

    let toolbar: { id: string; onClick: () => void }[] = [];
    render(
      <ProfileSubject
        args={{ subject: "profile", subjectId: "reviewer" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(actions) => {
          toolbar = actions;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    const checkbox = screen.getByLabelText("Claude dangerously skip permissions");
    expect(checkbox).toBeChecked();
    expect(screen.getByLabelText("Permission mode")).toHaveValue("");

    await userEvent.click(checkbox);
    await waitFor(() => expect(toolbar.find((action) => action.id === "save")).toBeDefined());
    toolbar.find((action) => action.id === "save")!.onClick();

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith(
        expect.objectContaining({
          permission_mode: null,
          claude_dangerously_skip_permissions: false,
        }),
      ),
    );
  });
});
