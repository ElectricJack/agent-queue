import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import ContextualSettingsPane from "../index";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("ContextualSettingsPane", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "useIntelligenceClasses").mockReturnValue({
      data: { success: true, classes: [] },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useIntelligenceClasses>);
  });

  it("dispatches to IntelligenceClassSubject for subject: intelligence-class", () => {
    render(
      <ContextualSettingsPane
        args={{ subject: "intelligence-class", subjectId: "fast-off" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );
    expect(screen.getByText(/not found/)).toBeInTheDocument();
  });

  it("registers $mod-s and Escape shortcuts for editable subjects", () => {
    vi.spyOn(hooks, "usePlaybookSource").mockReturnValue({
      data: { path: "x.md", markdown: "hi", source_hash: "h" },
      isLoading: false,
    } as unknown as ReturnType<typeof hooks.usePlaybookSource>);
    vi.spyOn(hooks, "usePlaybooks").mockReturnValue({ data: [] } as unknown as ReturnType<typeof hooks.usePlaybooks>);
    vi.spyOn(hooks, "useUpdatePlaybookSource").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdatePlaybookSource>);

    const setShortcuts = vi.fn();
    render(
      <ContextualSettingsPane
        args={{ subject: "playbook", subjectId: "review-gate" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={setShortcuts}
      />,
      { wrapper },
    );

    const bindings = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1]![0];
    expect(bindings.map((b: { key: string }) => b.key)).toEqual(["$mod-s", "Escape"]);
  });

  it("does not register $mod-s for the read-only intelligence-class subject", () => {
    const setShortcuts = vi.fn();
    render(
      <ContextualSettingsPane
        args={{ subject: "intelligence-class", subjectId: "fast-off" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={setShortcuts}
      />,
      { wrapper },
    );

    const bindings = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1]?.[0] ?? [];
    expect(bindings.find((b: { key: string }) => b.key === "$mod-s")).toBeUndefined();
  });
});
