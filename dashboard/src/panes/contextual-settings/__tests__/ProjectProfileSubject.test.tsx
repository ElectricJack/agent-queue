import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import ProjectProfileSubject from "../subjects/ProjectProfileSubject";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("ProjectProfileSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
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
    vi.spyOn(hooks, "useEditProjectProfile").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useEditProjectProfile>);
  });

  it("seeds from scoped when present", () => {
    vi.spyOn(hooks, "useProjectProfiles").mockReturnValue({
      data: {
        agent_types: [
          {
            agent_type: "coder",
            scoped: { name: "Coder (demo)", description: "", default_class: "", permission_mode: "", system_prompt_suffix: "", allowed_tools: [], mcp_servers: [] },
            global: { name: "Coder", description: "", default_class: "", permission_mode: "", system_prompt_suffix: "", allowed_tools: [], mcp_servers: [] },
          },
        ],
      },
    } as unknown as ReturnType<typeof hooks.useProjectProfiles>);

    render(
      <ProjectProfileSubject
        args={{ subject: "project-profile", subjectId: "coder", projectId: "demo" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByLabelText("Name")).toHaveValue("Coder (demo)");
  });

  it("falls back to global and disables Save when no scoped override exists", () => {
    vi.spyOn(hooks, "useProjectProfiles").mockReturnValue({
      data: {
        agent_types: [
          {
            agent_type: "coder",
            scoped: null,
            global: { name: "Coder", description: "", default_class: "", permission_mode: "", system_prompt_suffix: "", allowed_tools: [], mcp_servers: [] },
          },
        ],
      },
    } as unknown as ReturnType<typeof hooks.useProjectProfiles>);

    let toolbar: { id: string; disabled?: boolean }[] = [];
    render(
      <ProjectProfileSubject
        args={{ subject: "project-profile", subjectId: "coder", projectId: "demo" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(a) => {
          toolbar = a;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByLabelText("Name")).toHaveValue("Coder");
    expect(screen.getByText(/No project override exists yet/)).toBeInTheDocument();
    expect(toolbar.find((a) => a.id === "save")!.disabled).toBe(true);
  });
});
