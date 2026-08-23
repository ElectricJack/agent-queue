import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import * as taskFilesApi from "../../../api/taskFiles";
import { diffReviewChangesArgsSchema, manifest } from "../manifest";
import DiffReviewChangesPane from "../index";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

Object.assign(navigator, { clipboard: { writeText: vi.fn() } });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function noop() {}

// Array.prototype.at() needs an es2022+ lib target; this project's
// tsconfig targets ES2020, so index from the end manually instead.
function last<T>(arr: T[]): T {
  return arr[arr.length - 1]!;
}

function renderPane(
  args: { taskId: string; base?: string; filePath?: string } = { taskId: "t1" },
  overrides: Partial<{
    setToolbar: (actions: unknown[]) => void;
    setShortcuts: (bindings: { key: string; label: string; onFire: () => void }[]) => void;
  }> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <DiffReviewChangesPane
        args={args}
        close={noop}
        setArgs={noop}
        setToolbar={overrides.setToolbar ?? noop}
        setShortcuts={overrides.setShortcuts ?? noop}
      />
    </QueryClientProvider>,
  );
}

describe("diff-review-changes manifest", () => {
  it("id matches the directory name", () => {
    // Avoid node:path/node:url here — this project's tsconfig targets
    // ES2020 without @types/node, so plain string manipulation on
    // import.meta.url is used instead of fileURLToPath/dirname/basename.
    const parts = import.meta.url.split("/");
    const dir = parts[parts.length - 3];
    expect(manifest.id).toBe(dir);
    expect(manifest.id).toBe("diff-review-changes");
  });

  it("accepts the minimal valid args", () => {
    const result = diffReviewChangesArgsSchema.safeParse({ taskId: "t1" });
    expect(result.success).toBe(true);
  });

  it("accepts full valid args", () => {
    const result = diffReviewChangesArgsSchema.safeParse({
      taskId: "t1",
      base: "main",
      filePath: "a.ts",
    });
    expect(result.success).toBe(true);
  });

  it("rejects missing taskId", () => {
    const result = diffReviewChangesArgsSchema.safeParse({});
    expect(result.success).toBe(false);
  });

  it("rejects empty-string taskId", () => {
    const result = diffReviewChangesArgsSchema.safeParse({ taskId: "" });
    expect(result.success).toBe(false);
  });

  it("open_shortcut is a valid normalized $mod form", () => {
    expect(manifest.open_shortcut).toMatch(/^\$mod-(shift-)?[a-z0-9]$/i);
  });
});

describe("DiffReviewChangesPane layout", () => {
  it("renders a two-column layout at default width", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "a.ts", additions: 1, deletions: 0, status: "M" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });
    const { container } = renderPane();
    await screen.findByText("a.ts");
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).toContain("flex");
    expect(root.className).not.toContain("flex-col");
  });
});

describe("DiffReviewChangesPane data fetching", () => {
  it("fetches files for the given taskId and renders the list", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "src/api/scope.py", additions: 5, deletions: 2, status: "M" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });

    renderPane({ taskId: "task-42" });

    expect(await screen.findByText("src/api/scope.py")).toBeInTheDocument();
    expect(taskFilesApi.fetchTaskFiles).toHaveBeenCalledWith("task-42");
  });

  it("shows a loading placeholder before the file list resolves", () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockReturnValue(new Promise(() => {}));
    renderPane();
    expect(screen.getByText("Loading files…")).toBeInTheDocument();
  });
});

describe("DiffReviewChangesPane file selection", () => {
  function mockFiles() {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [
        { path: "src/api/scope.py", additions: 5, deletions: 2, status: "M" },
        { path: "README.md", additions: 1, deletions: 0, status: "A" },
      ],
      base: "main",
      workspace_path: "/tmp/ws",
    });
  }

  it("clicking a file row fetches and renders its content", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "def scope(): ...",
      status: 200,
    });

    renderPane();
    const row = await screen.findByText("src/api/scope.py");
    row.click();

    expect(await screen.findByText("def scope(): ...")).toBeInTheDocument();
    expect(taskFilesApi.fetchTaskFileText).toHaveBeenCalledWith("t1", "src/api/scope.py");
  });

  it("clicking a second row replaces the previewed content", async () => {
    mockFiles();
    const fetchText = vi.spyOn(taskFilesApi, "fetchTaskFileText");
    fetchText.mockResolvedValueOnce({ text: "first file body", status: 200 });
    fetchText.mockResolvedValueOnce({ text: "second file body", status: 200 });

    renderPane();
    (await screen.findByText("src/api/scope.py")).click();
    expect(await screen.findByText("first file body")).toBeInTheDocument();

    (await screen.findByText("README.md")).click();
    expect(await screen.findByText("second file body")).toBeInTheDocument();
    expect(screen.queryByText("first file body")).not.toBeInTheDocument();
  });

  it("renders .md files through MarkdownPreview", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "# Heading\n\nbody",
      status: 200,
    });

    renderPane();
    (await screen.findByText("README.md")).click();

    expect(await screen.findByRole("heading", { name: "Heading" })).toBeInTheDocument();
  });

  it("renders non-.md files as plain <pre> text, not MarkdownPreview", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "# not a heading, just text",
      status: 200,
    });

    renderPane();
    (await screen.findByText("src/api/scope.py")).click();

    expect(await screen.findByText("# not a heading, just text")).toBeInTheDocument();
    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });

  it("filePath arg pre-selects a matching file on mount", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "preselected body",
      status: 200,
    });

    renderPane({ taskId: "t1", filePath: "README.md" });

    expect(await screen.findByText("preselected body")).toBeInTheDocument();
  });

  it("filePath arg with no matching file leaves nothing selected", async () => {
    mockFiles();
    const fetchText = vi.spyOn(taskFilesApi, "fetchTaskFileText");

    renderPane({ taskId: "t1", filePath: "does/not/exist.ts" });

    await screen.findByText("src/api/scope.py");
    expect(screen.getByText("Select a file to preview.")).toBeInTheDocument();
    expect(fetchText).not.toHaveBeenCalled();
  });
});

describe("DiffReviewChangesPane toolbar", () => {
  function mockFiles() {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "a.ts", additions: 1, deletions: 0, status: "M" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });
  }

  it("registers Refresh, Copy file path, and Open full-page view", async () => {
    mockFiles();
    const setToolbar = vi.fn();
    renderPane({ taskId: "t1" }, { setToolbar });
    await screen.findByText("a.ts");

    const lastCall = last(setToolbar.mock.calls)[0] as { id: string; disabled?: boolean }[];
    const ids = lastCall.map((a) => a.id);
    expect(ids).toEqual(["refresh", "copy-path", "open-full-page"]);
  });

  it("Copy file path is disabled until a file is selected", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "file body",
      status: 200,
    });
    const setToolbar = vi.fn();
    renderPane({ taskId: "t1" }, { setToolbar });
    await screen.findByText("a.ts");

    let lastCall = last(setToolbar.mock.calls)[0] as { id: string; disabled?: boolean }[];
    expect(lastCall.find((a) => a.id === "copy-path")?.disabled).toBe(true);

    screen.getByText("a.ts").click();
    await screen.findByText(/./, { selector: "pre" });

    lastCall = last(setToolbar.mock.calls)[0] as { id: string; disabled?: boolean }[];
    expect(lastCall.find((a) => a.id === "copy-path")?.disabled).toBe(false);
  });

  it("Refresh re-runs the file-list fetch", async () => {
    mockFiles();
    const setToolbar = vi.fn();
    renderPane({ taskId: "t1" }, { setToolbar });
    await screen.findByText("a.ts");
    const callsBefore = (taskFilesApi.fetchTaskFiles as ReturnType<typeof vi.fn>).mock.calls
      .length;

    const lastCall = last(setToolbar.mock.calls)[0] as { id: string; onClick: () => void }[];
    lastCall.find((a) => a.id === "refresh")!.onClick();

    await vi.waitFor(() => {
      expect(
        (taskFilesApi.fetchTaskFiles as ReturnType<typeof vi.fn>).mock.calls.length,
      ).toBeGreaterThan(callsBefore);
    });
  });

  it("Open full-page view navigates to /tasks/:id/files", async () => {
    mockFiles();
    const setToolbar = vi.fn();
    renderPane({ taskId: "abc-123" }, { setToolbar });
    await screen.findByText("a.ts");

    const lastCall = last(setToolbar.mock.calls)[0] as { id: string; onClick: () => void }[];
    lastCall.find((a) => a.id === "open-full-page")!.onClick();

    expect(mockNavigate).toHaveBeenCalledWith("/tasks/abc-123/files");
  });
});

describe("DiffReviewChangesPane shortcuts and filtering", () => {
  function mockFiles() {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [
        { path: "src/a.ts", additions: 1, deletions: 0, status: "M" },
        { path: "src/b.ts", additions: 2, deletions: 1, status: "M" },
        { path: "README.md", additions: 3, deletions: 0, status: "A" },
      ],
      base: "main",
      workspace_path: "/tmp/ws",
    });
  }

  it("registers ArrowUp/ArrowDown/Enter//r bindings", async () => {
    mockFiles();
    const setShortcuts = vi.fn();
    renderPane({ taskId: "t1" }, { setShortcuts });
    await screen.findByText("src/a.ts");

    const lastCall = last(setShortcuts.mock.calls)[0] as { key: string; onFire: () => void }[];
    expect(lastCall.map((b) => b.key)).toEqual(["ArrowUp", "ArrowDown", "Enter", "/", "r"]);
  });

  it("ArrowDown then Enter opens the next file in the list", async () => {
    mockFiles();
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "b body",
      status: 200,
    });
    const setShortcuts = vi.fn();
    renderPane({ taskId: "t1" }, { setShortcuts });
    await screen.findByText("src/a.ts");

    const bindings = last(setShortcuts.mock.calls)[0] as { key: string; onFire: () => void }[];
    bindings.find((b) => b.key === "ArrowDown")!.onFire();
    bindings.find((b) => b.key === "Enter")!.onFire();

    expect(await screen.findByText("b body")).toBeInTheDocument();
  });

  it("/ shortcut focuses the filter input", async () => {
    mockFiles();
    const setShortcuts = vi.fn();
    renderPane({ taskId: "t1" }, { setShortcuts });
    await screen.findByText("src/a.ts");

    const bindings = last(setShortcuts.mock.calls)[0] as { key: string; onFire: () => void }[];
    bindings.find((b) => b.key === "/")!.onFire();

    expect(document.activeElement).toBe(screen.getByPlaceholderText("Filter files…"));
  });

  it("r shortcut refetches the file list", async () => {
    mockFiles();
    const setShortcuts = vi.fn();
    renderPane({ taskId: "t1" }, { setShortcuts });
    await screen.findByText("src/a.ts");
    const callsBefore = (taskFilesApi.fetchTaskFiles as ReturnType<typeof vi.fn>).mock.calls
      .length;

    const bindings = last(setShortcuts.mock.calls)[0] as { key: string; onFire: () => void }[];
    bindings.find((b) => b.key === "r")!.onFire();

    await vi.waitFor(() => {
      expect(
        (taskFilesApi.fetchTaskFiles as ReturnType<typeof vi.fn>).mock.calls.length,
      ).toBeGreaterThan(callsBefore);
    });
  });

  it("typing in the filter input narrows the list by case-insensitive substring", async () => {
    mockFiles();
    renderPane();
    await screen.findByText("src/a.ts");

    const input = screen.getByPlaceholderText("Filter files…");
    await userEvent.type(input, "README");

    expect(screen.getByText("README.md")).toBeInTheDocument();
    expect(screen.queryByText("src/a.ts")).not.toBeInTheDocument();
    expect(screen.queryByText("src/b.ts")).not.toBeInTheDocument();
  });
});

describe("DiffReviewChangesPane loading and error states", () => {
  it("shows the top-level error branch when the file list fetch rejects", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockRejectedValue(new Error("boom"));
    renderPane();
    expect(await screen.findByText("Failed to load files: boom")).toBeInTheDocument();
  });

  it("shows the no_workspace message and no file list", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [],
      base: null,
      workspace_path: null,
      reason: "no_workspace",
    });
    renderPane();
    expect(
      await screen.findByText(
        "Task has no attached workspace. Files will appear once the task acquires a worktree.",
      ),
    ).toBeInTheDocument();
  });

  it("shows the not_a_git_checkout message with workspace_path interpolated", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [],
      base: null,
      workspace_path: "/tmp/not-git",
      reason: "not_a_git_checkout",
    });
    renderPane();
    expect(
      await screen.findByText("Task workspace (/tmp/not-git) is not a git checkout."),
    ).toBeInTheDocument();
  });

  it("shows the empty-diff message with base interpolated", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [],
      base: "main",
      workspace_path: "/tmp/ws",
    });
    renderPane();
    expect(await screen.findByText("No changes vs main yet.")).toBeInTheDocument();
  });

  it("renders a binary-file placeholder through the plain <pre> branch", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "logo.png", additions: 0, deletions: 0, status: "A" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "(binary file omitted (12 KB))",
      status: 200,
    });
    renderPane();
    (await screen.findByText("logo.png")).click();
    expect(await screen.findByText("(binary file omitted (12 KB))")).toBeInTheDocument();
  });

  it("renders a forbidden-path placeholder through the plain <pre> branch", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "secret.env", additions: 0, deletions: 0, status: "A" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });
    vi.spyOn(taskFilesApi, "fetchTaskFileText").mockResolvedValue({
      text: "(forbidden path)",
      status: 403,
    });
    renderPane();
    (await screen.findByText("secret.env")).click();
    expect(await screen.findByText("(forbidden path)")).toBeInTheDocument();
  });
});

describe("DiffReviewChangesPane narrow-pane collapse", () => {
  it("switches to a stacked layout when the container is narrower than 400px", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "a.ts", additions: 1, deletions: 0, status: "M" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });

    let capturedCallback: ResizeObserverCallback | null = null;
    class CapturingResizeObserver {
      constructor(cb: ResizeObserverCallback) {
        capturedCallback = cb;
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    // Save/restore manually rather than vi.stubGlobal/unstubAllGlobals — in
    // this project's vitest config (isolate: false, singleFork) unstubbing
    // has been observed to leave `ResizeObserver` fully undefined instead of
    // restoring the setupTests.ts polyfill, breaking every test that runs
    // after this one in the same process.
    const originalResizeObserver = globalThis.ResizeObserver;
    (globalThis as { ResizeObserver: unknown }).ResizeObserver = CapturingResizeObserver;

    try {
      const { container } = renderPane();
      await screen.findByText("a.ts");

      const root = container.firstElementChild as HTMLElement;
      expect(root.className).not.toContain("flex-col");

      capturedCallback!(
        [{ contentRect: { width: 300 } } as ResizeObserverEntry],
        undefined as unknown as ResizeObserver,
      );

      await vi.waitFor(() => {
        expect(root.className).toContain("flex-col");
      });
    } finally {
      (globalThis as { ResizeObserver: unknown }).ResizeObserver = originalResizeObserver;
    }
  });
});

describe("DiffReviewChangesPane close prop", () => {
  it("accepts the close prop without invoking it", async () => {
    vi.spyOn(taskFilesApi, "fetchTaskFiles").mockResolvedValue({
      success: true,
      files: [{ path: "a.ts", additions: 1, deletions: 0, status: "M" }],
      base: "main",
      workspace_path: "/tmp/ws",
    });
    const close = vi.fn();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <DiffReviewChangesPane
          args={{ taskId: "t1" }}
          close={close}
          setArgs={noop}
          setToolbar={noop}
          setShortcuts={noop}
        />
      </QueryClientProvider>,
    );
    await screen.findByText("a.ts");
    expect(close).not.toHaveBeenCalled();
  });
});
