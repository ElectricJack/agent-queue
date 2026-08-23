import { describe, expect, it, vi, beforeEach } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import PlaybookSubject from "../subjects/PlaybookSubject";
import * as hooks from "../../../api/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

const source = { path: "vault/playbooks/review-gate.md", markdown: "# review-gate\n", source_hash: "abc123" };

describe("PlaybookSubject", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(hooks, "usePlaybookSource").mockReturnValue({
      data: source,
      isLoading: false,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof hooks.usePlaybookSource>);
    vi.spyOn(hooks, "usePlaybooks").mockReturnValue({
      data: [{ id: "review-gate", scope: "system", version: 1, node_count: 3, triggers: ["task.closed"] }],
    } as unknown as ReturnType<typeof hooks.usePlaybooks>);
  });

  it("renders the textarea seeded from source.markdown", () => {
    vi.spyOn(hooks, "useUpdatePlaybookSource").mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdatePlaybookSource>);

    render(
      <PlaybookSubject
        args={{ subject: "playbook", subjectId: "review-gate" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={vi.fn()}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    expect(screen.getByLabelText("Playbook source")).toHaveValue("# review-gate\n");
  });

  it("save calls useUpdatePlaybookSource with the loaded hash", async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ compiled: true, version: 2, node_count: 3, source_hash: "def456" });
    vi.spyOn(hooks, "useUpdatePlaybookSource").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdatePlaybookSource>);

    let toolbar: { id: string; onClick: () => void }[] = [];
    render(
      <PlaybookSubject
        args={{ subject: "playbook", subjectId: "review-gate" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(a) => {
          toolbar = a;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    await userEvent.type(screen.getByLabelText("Playbook source"), "\n# more");
    await waitFor(() => expect(toolbar.find((a) => a.id === "save")).toBeDefined());
    toolbar.find((a) => a.id === "save")!.onClick();

    await waitFor(() =>
      expect(mutateAsync).toHaveBeenCalledWith({
        playbook_id: "review-gate",
        markdown: "# review-gate\n\n# more",
        expected_source_hash: "abc123",
      }),
    );
  });

  it("a conflict response surfaces without clobbering the draft", async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ compiled: false, error: "conflict", errors: null });
    vi.spyOn(hooks, "useUpdatePlaybookSource").mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof hooks.useUpdatePlaybookSource>);

    let toolbar: { id: string; onClick: () => void }[] = [];
    render(
      <PlaybookSubject
        args={{ subject: "playbook", subjectId: "review-gate" }}
        close={vi.fn()}
        setArgs={vi.fn()}
        setToolbar={(a) => {
          toolbar = a;
        }}
        setShortcuts={vi.fn()}
      />,
      { wrapper },
    );

    await userEvent.type(screen.getByLabelText("Playbook source"), "!");
    await waitFor(() => expect(toolbar.find((a) => a.id === "save")).toBeDefined());
    toolbar.find((a) => a.id === "save")!.onClick();

    await waitFor(() => expect(screen.getByText(/Vault changed underneath this editor/)).toBeInTheDocument());
    expect(screen.getByLabelText("Playbook source")).toHaveValue("# review-gate\n!");
  });
});
