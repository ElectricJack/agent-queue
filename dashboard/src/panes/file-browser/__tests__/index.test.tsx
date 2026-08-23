import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as workspaceFilesApi from "../../../api/workspaceFiles";
import { fileBrowserArgsSchema, manifest } from "../manifest";
import FileBrowserPane from "../index";
import type { FileBrowserArgs } from "../manifest";

Object.assign(navigator, { clipboard: { writeText: vi.fn() } });

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function noop() {}

function renderPane(
  args: FileBrowserArgs,
  overrides: Partial<{
    setArgs: (next: FileBrowserArgs) => void;
    setToolbar: (actions: unknown[]) => void;
    setShortcuts: (bindings: { key: string; label: string; onFire: () => void }[]) => void;
  }> = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <FileBrowserPane
        args={args}
        close={noop}
        setArgs={overrides.setArgs ?? noop}
        setToolbar={overrides.setToolbar ?? noop}
        setShortcuts={overrides.setShortcuts ?? noop}
      />
    </QueryClientProvider>,
  );
}

const browseRoot: workspaceFilesApi.WorkspaceBrowseResponse = {
  success: true,
  path: "",
  entries: [
    { name: "src", type: "dir" },
    { name: "README.md", type: "file", size: 12 },
  ],
};

describe("file-browser manifest", () => {
  it("id matches the directory name", () => {
    expect(manifest.id).toBe("file-browser");
  });

  it("args schema accepts workspaceId with default path", () => {
    const parsed = fileBrowserArgsSchema.parse({ workspaceId: "ws1" });
    expect(parsed).toEqual({ workspaceId: "ws1", path: "" });
  });

  it("args schema accepts workspaceId + explicit path", () => {
    const parsed = fileBrowserArgsSchema.parse({ workspaceId: "ws1", path: "a/b" });
    expect(parsed).toEqual({ workspaceId: "ws1", path: "a/b" });
  });

  it("args schema rejects missing workspaceId", () => {
    expect(() => fileBrowserArgsSchema.parse({})).toThrow();
  });

  it("args schema rejects non-string workspaceId", () => {
    expect(() => fileBrowserArgsSchema.parse({ workspaceId: 5 })).toThrow();
  });

  it("open_shortcut is a valid normalized $mod form", () => {
    expect(manifest.open_shortcut).toMatch(/^\$mod-(shift-)?[a-z0-9]$/i);
  });
});

describe("FileBrowserPane tree", () => {
  it("renders tree from mocked browse response (dirs before files)", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue(browseRoot);
    renderPane({ workspaceId: "ws1", path: "" });

    await screen.findByText("src");
    const names = screen.getAllByText(/^(src|README\.md)$/).map((el) => el.textContent);
    expect(names).toEqual(["src", "README.md"]);
  });

  it("directory click calls setArgs with new path, same workspaceId", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue(browseRoot);
    const setArgs = vi.fn();
    renderPane({ workspaceId: "ws1", path: "" }, { setArgs });

    await screen.findByText("src");
    fireEvent.click(screen.getByText("src"));

    expect(setArgs).toHaveBeenCalledWith({ workspaceId: "ws1", path: "src" });
  });

  it("breadcrumb segment click calls setArgs with that path; root clears to empty", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue({
      success: true,
      path: "a/b",
      entries: [],
    });
    const setArgs = vi.fn();
    renderPane({ workspaceId: "ws1", path: "a/b" }, { setArgs });

    await screen.findByText("This directory is empty.");
    fireEvent.click(screen.getByText("a"));
    expect(setArgs).toHaveBeenCalledWith({ workspaceId: "ws1", path: "a" });

    fireEvent.click(screen.getByText("root"));
    expect(setArgs).toHaveBeenCalledWith({ workspaceId: "ws1", path: "" });
  });

  it("up-navigation: disabled at root; at path 'a/b' calls setArgs with 'a'", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue({
      success: true,
      path: "a/b",
      entries: [],
    });
    const setArgs = vi.fn();
    const setToolbar = vi.fn();
    renderPane({ workspaceId: "ws1", path: "a/b" }, { setArgs, setToolbar });

    await waitFor(() => {
      const lastCall = setToolbar.mock.calls[setToolbar.mock.calls.length - 1];
      const up = (lastCall?.[0] as { id: string; onClick: () => void; disabled?: boolean }[]).find(
        (a) => a.id === "up",
      );
      expect(up?.disabled).toBe(false);
      up?.onClick();
    });
    expect(setArgs).toHaveBeenCalledWith({ workspaceId: "ws1", path: "a" });
  });

  it("Up one dir and Open workspace root are disabled at path=''", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue(browseRoot);
    const setToolbar = vi.fn();
    renderPane({ workspaceId: "ws1", path: "" }, { setToolbar });

    await waitFor(() => {
      const lastCall = setToolbar.mock.calls[setToolbar.mock.calls.length - 1];
      const ids = (lastCall?.[0] as { id: string; disabled?: boolean }[]) ?? [];
      expect(ids.find((a) => a.id === "up")?.disabled).toBe(true);
      expect(ids.find((a) => a.id === "root")?.disabled).toBe(true);
    });
  });

  it("setToolbar is called with four actions", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue(browseRoot);
    const setToolbar = vi.fn();
    renderPane({ workspaceId: "ws1", path: "" }, { setToolbar });

    await waitFor(() => {
      const lastCall = setToolbar.mock.calls[setToolbar.mock.calls.length - 1];
      const ids = (lastCall?.[0] as { id: string }[]).map((a) => a.id);
      expect(ids).toEqual(["refresh", "copy-path", "up", "root"]);
    });
  });

  it("setShortcuts is called with Backspace, /, r", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue(browseRoot);
    const setShortcuts = vi.fn();
    renderPane({ workspaceId: "ws1", path: "" }, { setShortcuts });

    await waitFor(() => {
      const lastCall = setShortcuts.mock.calls[setShortcuts.mock.calls.length - 1];
      const keys = (lastCall?.[0] as { key: string }[]).map((b) => b.key);
      expect(keys).toEqual(["Backspace", "/", "r"]);
    });
  });

  it("empty directory shows the empty-state message", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue({
      success: true,
      path: "empty",
      entries: [],
    });
    renderPane({ workspaceId: "ws1", path: "empty" });

    await screen.findByText("This directory is empty.");
  });

  it("reason: no_workspace_path renders the correct message without crashing", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue({
      success: true,
      path: "",
      entries: [],
      reason: "no_workspace_path",
    });
    renderPane({ workspaceId: "ws-empty", path: "" });

    await screen.findByText(
      "This workspace has no filesystem path yet — nothing to browse.",
    );
  });

  it("filter hides non-matching rows client-side with no new network call", async () => {
    const spy = vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue(browseRoot);
    renderPane({ workspaceId: "ws1", path: "" });

    await screen.findByText("src");
    const callCountAfterLoad = spy.mock.calls.length;

    fireEvent.change(screen.getByPlaceholderText("Filter files…"), {
      target: { value: "readme" },
    });

    await waitFor(() => {
      expect(screen.queryByText("src")).not.toBeInTheDocument();
      expect(screen.getByText("README.md")).toBeInTheDocument();
    });
    expect(spy.mock.calls.length).toBe(callCountAfterLoad);
  });
});

describe("FileBrowserPane preview", () => {
  it("file click sets previewPath without calling setArgs", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue(browseRoot);
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceFileText").mockResolvedValue({
      text: "# hello",
      status: 200,
    });
    const setArgs = vi.fn();
    renderPane({ workspaceId: "ws1", path: "" }, { setArgs });

    await screen.findByText("README.md");
    fireEvent.click(screen.getByText("README.md"));

    await waitFor(() =>
      expect(screen.getByText("README.md", { selector: "p" })).toBeInTheDocument(),
    );
    expect(setArgs).not.toHaveBeenCalled();
  });

  it("renders markdown for .md files", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue(browseRoot);
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceFileText").mockResolvedValue({
      text: "# Hello",
      status: 200,
    });
    renderPane({ workspaceId: "ws1", path: "" });

    await screen.findByText("README.md");
    fireEvent.click(screen.getByText("README.md"));

    expect(await screen.findByRole("heading", { name: "Hello" })).toBeInTheDocument();
  });

  it("preview switching: select file A then file B without navigating", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue({
      success: true,
      path: "",
      entries: [
        { name: "a.txt", type: "file", size: 1 },
        { name: "b.txt", type: "file", size: 1 },
      ],
    });
    const fileSpy = vi.spyOn(workspaceFilesApi, "fetchWorkspaceFileText");
    fileSpy.mockImplementation(async (_ws, path) => ({ text: `content of ${path}`, status: 200 }));
    renderPane({ workspaceId: "ws1", path: "" });

    await screen.findByText("a.txt");
    fireEvent.click(screen.getByText("a.txt"));
    await screen.findByText("content of a.txt");

    fireEvent.click(screen.getByText("b.txt"));
    await screen.findByText("content of b.txt");
    expect(screen.queryByText("content of a.txt")).not.toBeInTheDocument();
  });

  it("binary file renders the binary placeholder instead of raw content", async () => {
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse").mockResolvedValue({
      success: true,
      path: "",
      entries: [{ name: "logo.png", type: "file", size: 4096 }],
    });
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceFileText").mockResolvedValue({
      text: "(binary file, size: 4 KB) — preview not available",
      status: 200,
    });
    renderPane({ workspaceId: "ws1", path: "" });

    await screen.findByText("logo.png");
    fireEvent.click(screen.getByText("logo.png"));

    await screen.findByText("(binary file, size: 4 KB) — preview not available");
  });

  it("mount-time file-push fallback: browse 404s not-a-directory, parent succeeds, file auto-previews", async () => {
    const browseSpy = vi.spyOn(workspaceFilesApi, "fetchWorkspaceBrowse");
    browseSpy.mockImplementation(async (_ws, path) => {
      if (path === "README.md") throw new Error("not a directory");
      return {
        success: true,
        path: "",
        entries: [{ name: "README.md", type: "file", size: 12 }],
      };
    });
    vi.spyOn(workspaceFilesApi, "fetchWorkspaceFileText").mockResolvedValue({
      text: "# hello",
      status: 200,
    });
    renderPane({ workspaceId: "ws1", path: "README.md" });

    await waitFor(() =>
      expect(screen.getByText("README.md", { selector: "p" })).toBeInTheDocument(),
    );
  });
});

// Regression: these panes published toolbar/shortcuts from the render body.
// `setToolbar`/`setShortcuts` are ShellPaneHost useState setters, so a
// render-phase call with a fresh array literal re-rendered the parent, which
// re-rendered the pane, which published again — an unbounded loop that froze
// the browser tab. See task-detail for the original report.
describe("file-browser — publishing is effect-scoped", () => {
  it("does not re-publish the toolbar when the pane re-renders", async () => {
    const setToolbar = vi.fn();
    const setShortcuts = vi.fn();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

    function Harness() {
      const [n, setN] = useState(0);
      return (
        <>
          <button onClick={() => setN(n + 1)}>bump {n}</button>
          <FileBrowserPane
            args={{ workspaceId: "ws1", path: "" }}
            close={noop}
            setArgs={noop}
            setToolbar={setToolbar}
            setShortcuts={setShortcuts}
          />
        </>
      );
    }

    render(
      <QueryClientProvider client={queryClient}>
        <Harness />
      </QueryClientProvider>,
    );
    const toolbarCalls = setToolbar.mock.calls.length;
    const shortcutCalls = setShortcuts.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: /bump/i }));

    expect(setToolbar.mock.calls.length).toBe(toolbarCalls);
    expect(setShortcuts.mock.calls.length).toBe(shortcutCalls);
  });
});
