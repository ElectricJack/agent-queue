import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import IntelligenceClassSubject from "../subjects/IntelligenceClassSubject";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("IntelligenceClassSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "useIntelligenceClasses").mockReturnValue({
      data: {
        success: true,
        classes: [
          { id: "fast-off", name: "Fast", description: "Quick, cheap.", mapping: { anthropic: { model: "haiku" } } },
          { id: "deep-high", name: "Deep", description: "Slow, thorough.", mapping: {} },
        ],
      },
      isLoading: false,
      error: null,
    } as unknown as ReturnType<typeof hooks.useIntelligenceClasses>);
  });

  it("renders only the matching class and links to editable dashboard settings", () => {
    let toolbar: { id: string }[] = [];
    render(
      <IntelligenceClassSubject
        args={{ subject: "intelligence-class", subjectId: "fast-off" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(a) => {
          toolbar = a;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByText("Fast")).toBeInTheDocument();
    expect(screen.queryByText("Deep")).not.toBeInTheDocument();
    expect(screen.getByText(/anthropic/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Edit in Intelligence Classes" })).toHaveAttribute("href", "/settings/intelligence-classes");
    expect(toolbar).toEqual([{ id: "open-full", label: "Open full settings page", icon: expect.anything(), onClick: expect.any(Function) }]);
  });

  it("renders a not-found message for an id absent from the list", () => {
    render(
      <IntelligenceClassSubject
        args={{ subject: "intelligence-class", subjectId: "missing" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByText('Intelligence class "missing" not found.')).toBeInTheDocument();
  });
});
