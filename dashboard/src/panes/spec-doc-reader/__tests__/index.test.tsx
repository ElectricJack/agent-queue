import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import SpecDocReaderPane, { MetaCard } from "../index";
import type { PaneViewProps } from "../../types";
import type { SpecDocReaderArgs } from "../manifest";

function baseProps(args: SpecDocReaderArgs): PaneViewProps<SpecDocReaderArgs> {
  return {
    args,
    close: vi.fn(),
    setArgs: vi.fn(),
    setToolbar: vi.fn(),
    setShortcuts: vi.fn(),
  };
}

function lastCallArg0(fn: ReturnType<typeof vi.fn>) {
  const calls = fn.mock.calls;
  const last = calls[calls.length - 1];
  if (!last) throw new Error("expected fn to have been called at least once");
  return last[0];
}

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("SpecDocReaderPane — TOC", () => {
  it("renders without crashing given valid args", () => {
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
  });
});

describe("MetaCard", () => {
  it("renders status as a pill, date as-is, companions as buttons, and unknown keys as rows", () => {
    render(
      <MetaCard
        data={{ status: "design", date: "2026-08-22", companions: "other.md", extra: "value" }}
        onCompanionClick={vi.fn()}
      />,
    );
    expect(screen.getByText("design")).toBeInTheDocument();
    expect(screen.getByText("2026-08-22")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "other.md" })).toBeInTheDocument();
    expect(screen.getByText("extra")).toBeInTheDocument();
    expect(screen.getByText("value")).toBeInTheDocument();
  });

  it("renders nothing when data is null", () => {
    const { container } = render(<MetaCard data={null} onCompanionClick={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });
});

describe("SpecDocReaderPane — local-file mode", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.includes("/api/workspaces/")) {
          // GET /api/workspaces/{id}/file returns raw text/plain — not a
          // JSON envelope (see hooks.ts's useWorkspaceFile doc comment).
          return new Response("---\nstatus: design\n---\n\n# X\n\n## Goal\n\nbody\n", {
            status: 200,
            headers: { "content-type": "text/plain; charset=utf-8" },
          });
        }
        return new Response("not found", { status: 404 });
      }),
    );
  });

  it("renders title + meta card + TOC + body once loaded", async () => {
    renderWithQuery(<SpecDocReaderPane {...baseProps({ workspaceId: "ws-1", path: "docs/x.md" })} />);
    expect(await screen.findByText("X")).toBeInTheDocument();
    expect(await screen.findByText("design")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Goal" })).toBeInTheDocument();
    expect(await screen.findByText("body")).toBeInTheDocument();
  });
});

describe("SpecDocReaderPane — url mode", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("# Hosted Doc\n\nhello\n", { status: 200 })));
  });

  it("renders content from a mocked same-origin fetch", async () => {
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
    expect(await screen.findByText("Hosted Doc")).toBeInTheDocument();
    expect(await screen.findByText("hello")).toBeInTheDocument();
  });
});

describe("SpecDocReaderPane — toolbar", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("# Doc\n\nbody\n", { status: 200 })));
    Object.assign(navigator, { clipboard: { writeText: vi.fn() } });
  });

  it("registers Copy URL + no Open-in-editor in url mode", async () => {
    const props = baseProps({ url: "/api/specs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    expect(props.setToolbar).toHaveBeenCalled();
    const actions = lastCallArg0(props.setToolbar as ReturnType<typeof vi.fn>);
    expect(actions.map((a: { label: string }) => a.label)).toEqual(["Copy URL"]);
  });

  it("Copy path/url copies the url in url mode", async () => {
    const props = baseProps({ url: "/api/specs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    const actions = lastCallArg0(props.setToolbar as ReturnType<typeof vi.fn>);
    actions[0].onClick();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("/api/specs/x.md");
  });

  it("registers Copy path + Open in editor in local-file mode", async () => {
    const props = baseProps({ workspaceId: "ws-1", path: "docs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    const actions = lastCallArg0(props.setToolbar as ReturnType<typeof vi.fn>);
    expect(actions.map((a: { label: string }) => a.label)).toEqual(["Copy path", "Open in editor"]);
  });

  it("Open in editor copies the path", async () => {
    const props = baseProps({ workspaceId: "ws-1", path: "docs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    const actions = lastCallArg0(props.setToolbar as ReturnType<typeof vi.fn>);
    const openInEditor = actions.find((a: { id: string }) => a.id === "open-in-editor");
    openInEditor.onClick();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith("docs/x.md");
  });
});

describe("SpecDocReaderPane — shortcuts + TOC interaction", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("# Doc\n\n## Goal\n\nbody\n\n## Non-goals\n\nmore\n", { status: 200 })),
    );
    Element.prototype.scrollIntoView = vi.fn();
  });

  it("registers scroll, page, and TOC shortcuts", async () => {
    const props = baseProps({ url: "/api/specs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    const bindings = lastCallArg0(props.setShortcuts as ReturnType<typeof vi.fn>);
    const keys = bindings.map((b: { key: string }) => b.key);
    expect(keys).toEqual(expect.arrayContaining(["ArrowUp", "ArrowDown", "j", "k", "PageUp", "PageDown", "t"]));
  });

  it("clicking a TOC entry scrolls into view without touching location.hash", async () => {
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
    const before = window.location.hash;
    const entry = await screen.findByRole("button", { name: "Goal" });
    entry.click();
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
    expect(window.location.hash).toBe(before);
  });

  it("t focuses the TOC region", async () => {
    const props = baseProps({ url: "/api/specs/x.md" });
    renderWithQuery(<SpecDocReaderPane {...props} />);
    await screen.findByText("Doc");
    const bindings = lastCallArg0(props.setShortcuts as ReturnType<typeof vi.fn>);
    const tBinding = bindings.find((b: { key: string }) => b.key === "t");
    tBinding.onFire();
    const nav = screen.getByRole("navigation", { name: "Table of contents" });
    expect(nav.contains(document.activeElement)).toBe(true);
  });
});

describe("SpecDocReaderPane — loading + error + edge cases", () => {
  it("shows a loading skeleton while the query is pending", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {}))); // never resolves
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
    expect(screen.getByTestId("spec-doc-reader-skeleton")).toBeInTheDocument();
  });

  it("renders a not-found state with the path in the message on 404", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("nope", { status: 404 })));
    renderWithQuery(<SpecDocReaderPane {...baseProps({ workspaceId: "ws-1", path: "docs/missing.md" })} />);
    expect(await screen.findByText(/Spec not found/)).toBeInTheDocument();
    expect(await screen.findByText("docs/missing.md")).toBeInTheDocument();
  });

  it("renders a 403 access-denied state with no retry button", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("forbidden", { status: 403 })));
    renderWithQuery(<SpecDocReaderPane {...baseProps({ workspaceId: "ws-1", path: "docs/x.md" })} />);
    expect(await screen.findByText(/don't have access/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("renders a generic error with a working Retry on 5xx", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("boom", { status: 500 }))
      .mockResolvedValueOnce(new Response("# Doc\n\nbody\n", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
    const retry = await screen.findByRole("button", { name: "Retry" });
    retry.click();
    expect(await screen.findByText("Doc")).toBeInTheDocument();
  });

  it("renders a <pre> fallback for non-markdown content with no TOC/meta card", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("plain text, no headings", { status: 200 })));
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/notes.txt" })} />);
    const pre = await screen.findByText("plain text, no headings");
    expect(pre.tagName).toBe("PRE");
    expect(screen.queryByRole("navigation", { name: "Table of contents" })).toBeNull();
  });

  it("renders the empty state when the post-frontmatter body is empty", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("---\nstatus: design\n---\n", { status: 200 })));
    renderWithQuery(<SpecDocReaderPane {...baseProps({ url: "/api/specs/x.md" })} />);
    expect(await screen.findByText("This document is empty.")).toBeInTheDocument();
  });
});
