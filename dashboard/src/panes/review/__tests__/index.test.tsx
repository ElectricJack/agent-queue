import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import ReviewPaneView from "../index";
import { reviewMarkdownFilename } from "../download";

const hooks = vi.hoisted(() => ({
  useReview: vi.fn(),
  comment: { mutateAsync: vi.fn().mockResolvedValue({}) },
  decide: { mutateAsync: vi.fn().mockResolvedValue({}), isPending: false },
  importEdits: { mutateAsync: vi.fn().mockResolvedValue({}), isPending: false },
  attach: { mutateAsync: vi.fn().mockResolvedValue({}), isPending: false },
  reopen: { mutateAsync: vi.fn().mockResolvedValue({ revision: 3 }), isPending: false },
  withdraw: { mutateAsync: vi.fn().mockResolvedValue({}), isPending: false },
  listener: null as ((event: { event_type: string; review_id?: string }) => void) | null,
}));

vi.mock("../../../api/reviews", () => ({
  useReview: hooks.useReview,
  useCommentReview: () => hooks.comment,
  useDecideReview: () => hooks.decide,
  useImportReviewEdits: () => hooks.importEdits,
  useAttachReviewImage: () => hooks.attach,
  useReopenReview: () => hooks.reopen,
  useWithdrawReview: () => hooks.withdraw,
}));
vi.mock("../../../api/hooks", () => ({
  useIntelligenceClasses: () => ({ data: { classes: [
    { id: "fast-high" }, { id: "standard-high" },
  ] } }),
  useProfiles: () => ({ data: [
    { id: "standard-high-codex", default_class: "standard-high", enabled: true },
  ] }),
}));
vi.mock("../../../ws/useEventStream", () => ({
  useRawEventSubscription: (listener: typeof hooks.listener) => { hooks.listener = listener; },
}));

const response = {
  review: { id: "rev-x", title: "Review title", kind: "spec", state: "in_review", current_revision: 2, decider: "user" },
  revision: { revision: 2, content: "---\nstatus: draft\n---\n# Review title\n\n## Goal\n\nVisible body", changes_note: "Expanded the goal" },
  revisions: [{ revision: 1 }, { revision: 2, changes_note: "Expanded the goal" }],
  vault_state: "ok",
  response_route: {
    kind: "new_task",
    summary: "Request changes → new revision task, routed by the project's router (no class hint); Approve → sent to the supervisor.",
    class_summaries: {
      "standard-high": "Request changes → new revision task, routed by the project's router (class hint standard-high); Approve → sent to the supervisor.",
    },
  },
  comments: [],
  diff: [{ op: "removed", text: "old paragraph" }, { op: "added", text: "new paragraph" }],
};

function renderPane() {
  return render(
    <MemoryRouter>
      <ReviewPaneView args={{ reviewId: "rev-x" }} close={vi.fn()} setArgs={vi.fn()} setToolbar={vi.fn()} setShortcuts={vi.fn()} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  hooks.listener = null;
  hooks.useReview.mockImplementation((_id: string, opts?: { diffFrom?: number }) => ({
    data: opts?.diffFrom === 1 ? response : { ...response, diff: undefined },
    isLoading: false,
    error: null,
  }));
});

describe("review pane", () => {
  it("shows withdrawal audit and dependents and reopens the displayed revision", async () => {
    hooks.useReview.mockReturnValue({ data: { ...response,
      review: { ...response.review, state: "withdrawn", withdrawn_by: "human:local-operator",
        withdrawn_via: "dashboard", withdrawn_at: 1791594413, withdrawal_reason: "Accidental" },
      dependent_task_ids: ["impl-1", "impl-2"], gate: { status: "cancelled" },
    } });
    renderPane();
    expect(screen.getByText(/Last withdrawn by human:local-operator via dashboard/)).toHaveTextContent("Accidental");
    expect(screen.getByRole("link", { name: "impl-1" })).toHaveAttribute("href", "/tasks/impl-1");
    fireEvent.click(screen.getByRole("button", { name: "Reopen review" }));
    await waitFor(() => expect(hooks.reopen.mutateAsync).toHaveBeenCalledWith({ review_id: "rev-x", revision: 2 }));
  });

  it("requires confirmation and permits cancelling withdrawal", async () => {
    renderPane();
    fireEvent.click(screen.getByRole("button", { name: "Close review" }));
    expect(screen.getByRole("dialog", { name: "Close review" })).toBeInTheDocument();
    expect(hooks.withdraw.mutateAsync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(hooks.withdraw.mutateAsync).not.toHaveBeenCalled();
  });
  describe("Markdown download", () => {
    let downloads: { filename: string; href: string; connected: boolean }[];

    beforeEach(() => {
      downloads = [];
      vi.stubGlobal("URL", class extends URL {
        static createObjectURL = vi.fn(() => "blob:review-markdown");
        static revokeObjectURL = vi.fn();
      });
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
        downloads.push({ filename: this.download, href: this.href, connected: this.isConnected });
      });
    });

    afterEach(async () => {
      await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledTimes(
        vi.mocked(URL.createObjectURL).mock.calls.length,
      ));
      vi.restoreAllMocks();
      vi.unstubAllGlobals();
    });

    async function downloadedText() {
      const blob = vi.mocked(URL.createObjectURL).mock.calls[0]![0] as Blob;
      expect(blob.type).toBe("text/markdown;charset=utf-8");
      return new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result as string);
        reader.onerror = reject;
        reader.readAsText(blob);
      });
    }

    it.each(["spec", "plan", "other"])("downloads exact raw %s Markdown even with a diff shown", async (kind) => {
      const raw = "---\r\nstatus: draft\r\n---\r\n# Café 日本語 🐈\r\n\r\n## Goal\r\n\r\n**Raw** [link](https://example.test)  \r\n\r\n";
      hooks.useReview.mockReturnValue({
        data: { ...response, review: { ...response.review, kind }, revision: { revision: 2, content: raw } },
        isLoading: false, error: null,
      });
      renderPane();
      fireEvent.click(screen.getByLabelText("Changes since previous"));
      expect(screen.getByText("new paragraph")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "Download Markdown" }));
      expect(await downloadedText()).toBe(raw);
      expect(downloads).toEqual([{ filename: "Review title-rev-2.md", href: "blob:review-markdown", connected: true }]);
      expect(document.querySelector('a[download]')).toBeNull();
      await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:review-markdown"));
      expect(hooks.decide.mutateAsync).not.toHaveBeenCalled();
      expect(hooks.comment.mutateAsync).not.toHaveBeenCalled();
    });

    it("downloads the selected historical revision of a closed review", async () => {
      const historical = "# Original plan\n\nOnly in revision one.\n";
      hooks.useReview.mockImplementation((_id: string, opts?: { revision?: number }) => ({
        data: {
          ...response, review: { ...response.review, state: "approved" },
          revision: opts?.revision === 1 ? { revision: 1, content: historical } : response.revision,
        },
        isLoading: false, error: null,
      }));
      renderPane();
      fireEvent.change(screen.getByLabelText("Review revision"), { target: { value: "1" } });
      expect(screen.getByText("Only in revision one.")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
      fireEvent.click(screen.getByRole("button", { name: "Download Markdown" }));
      expect(await downloadedText()).toBe(historical);
      expect(downloads[0]?.filename).toBe("Review title-rev-1.md");
    });

    it("disables download while the response is for a different revision", () => {
      const { rerender } = renderPane();
      fireEvent.change(screen.getByLabelText("Review revision"), { target: { value: "1" } });
      const button = screen.getByRole("button", { name: "Download Markdown" });
      expect(button).toBeDisabled();
      fireEvent.click(button);
      expect(URL.createObjectURL).not.toHaveBeenCalled();
      hooks.useReview.mockReturnValue({
        data: { ...response, revision: { revision: 1, content: "# Historical" } },
        isLoading: false, error: null,
      });
      rerender(<MemoryRouter><ReviewPaneView args={{ reviewId: "rev-x" }} close={vi.fn()} setArgs={vi.fn()} setToolbar={vi.fn()} setShortcuts={vi.fn()} /></MemoryRouter>);
      expect(screen.getByRole("button", { name: "Download Markdown" })).toBeEnabled();
    });

    it("has no download action before review data loads", () => {
      hooks.useReview.mockReturnValue({ data: undefined, isLoading: true, error: null });
      renderPane();
      expect(screen.getByText("Loading review…")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Download Markdown" })).not.toBeInTheDocument();
    });

    it("disables unavailable content but permits an empty document", async () => {
      hooks.useReview.mockReturnValue({
        data: { ...response, revision: { revision: 2, content: undefined } },
        isLoading: false, error: null,
      });
      const { rerender } = renderPane();
      expect(screen.getByRole("button", { name: "Download Markdown" })).toBeDisabled();
      hooks.useReview.mockReturnValue({
        data: { ...response, revision: { revision: 2, content: "" } },
        isLoading: false, error: null,
      });
      rerender(<MemoryRouter><ReviewPaneView args={{ reviewId: "rev-x" }} close={vi.fn()} setArgs={vi.fn()} setToolbar={vi.fn()} setShortcuts={vi.fn()} /></MemoryRouter>);
      fireEvent.click(screen.getByRole("button", { name: "Download Markdown" }));
      expect(await downloadedText()).toBe("");
    });

    it.each([
      ["Café 日本語", "rev-x", "Café 日本語-rev-3.md"],
      ["../Plan\\draft:what?*<>|\"\u0000\u007f\u202e\r\n", "rev-x", "Plan-draft-what-rev-3.md"],
      [" .. ", "rev/backup\\id", "rev-backup-id-rev-3.md"],
      ["", "../\u0000", "review-rev-3.md"],
      ["x".repeat(200), "rev-x", `${"x".repeat(50)}-rev-3.md`],
      ["🐈".repeat(100), "rev-x", `${"🐈".repeat(50)}-rev-3.md`],
    ])("uses a safe readable filename for %j", (title, id, filename) => {
      expect(reviewMarkdownFilename(title, id, 3)).toBe(filename);
    });
  });

  it("renders the document title, metadata, TOC, and markdown body", () => {
    renderPane();
    expect(screen.getByRole("heading", { name: "Review title" })).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Goal" })).toBeInTheDocument();
    expect(screen.getByText("Visible body")).toBeInTheDocument();
  });

  it("renders the title once for CRLF content with YAML frontmatter", () => {
    const content = "---\r\nstatus: draft\r\n---\r\n# Original plan\r\n\r\n## Goal\r\n\r\nVisible body\r\n";
    hooks.useReview.mockImplementation(() => ({
      data: { ...response, revision: { ...response.revision, content }, diff: undefined },
      isLoading: false,
      error: null,
    }));
    renderPane();
    expect(screen.getAllByRole("heading", { name: "Original plan" })).toHaveLength(1);
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Goal" })).toBeInTheDocument();
    expect(screen.getByText("Visible body")).toBeInTheDocument();
  });

  it("renders images for the selected revision with pinned metadata", () => {
    hooks.useReview.mockImplementation(() => ({
      data: { ...response, attachments: [{
        id: "image-1", review_id: "rev-x", revision: 2,
        url: "/api/reviews/rev-x/revisions/2/attachments/image-1",
        sha256: "abc123", content_type: "image/png", size: 42,
        caption: "Front comparison", view_id: "front", candidate_id: "rock-7",
      }] }, isLoading: false, error: null,
    }));
    renderPane();
    expect(screen.getByRole("img", { name: "Front comparison" })).toHaveAttribute(
      "src", "/api/reviews/rev-x/revisions/2/attachments/image-1",
    );
    expect(screen.getByText("View front · Candidate rock-7")).toBeInTheDocument();
    expect(screen.getByText("SHA-256 abc123")).toBeInTheDocument();
  });

  it("pairs result images by view with plain captions and no internal metadata", () => {
    hooks.useReview.mockImplementation(() => ({
      data: { ...response, attachments: ["Back", "Front"].flatMap((view) =>
        ["After", "Before"].map((candidate) => ({
          id: `${view}-${candidate}`, review_id: "rev-x", revision: 2,
          url: `/${view}-${candidate}.png`, sha256: "internal-hash", content_type: "image/png",
          size: 42, caption: `${view} ${candidate}`, view_id: view, candidate_id: candidate,
        }))) }, isLoading: false, error: null,
    }));
    renderPane();
    expect(screen.getByRole("heading", { name: "Before and after" })).toBeInTheDocument();
    expect(screen.getAllByRole("img").map((img) => img.getAttribute("alt"))).toEqual([
      "Back Before", "Back After", "Front Before", "Front After",
    ]);
    expect(screen.queryByText(/SHA-256/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Candidate Before/)).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Front Before" }).closest("figure")?.parentElement)
      .toHaveClass("grid-cols-2");
  });

  it("submits an image with the viewed revision and view/candidate IDs", async () => {
    renderPane();
    fireEvent.change(screen.getByLabelText("Screenshot image"), {
      target: { files: [new File(["image bytes"], "front.png", { type: "image/png" })] },
    });
    fireEvent.change(screen.getByLabelText("Screenshot caption"), {
      target: { value: "Front comparison" },
    });
    fireEvent.change(screen.getByLabelText("View ID"), { target: { value: "front" } });
    fireEvent.change(screen.getByLabelText("Candidate ID"), { target: { value: "rock-7" } });
    fireEvent.submit(screen.getByRole("button", { name: "Attach screenshot" }).closest("form")!);
    await waitFor(() => expect(hooks.attach.mutateAsync).toHaveBeenCalledWith({
      review_id: "rev-x", revision: 2, data_base64: btoa("image bytes"),
      content_type: "image/png", caption: "Front comparison",
      view_id: "front", candidate_id: "rock-7",
    }));
  });

  it("requests the selected revision and its previous-revision diff", async () => {
    renderPane();
    fireEvent.change(screen.getByLabelText("Review revision"), { target: { value: "1" } });
    await waitFor(() => expect(hooks.useReview).toHaveBeenCalledWith("rev-x", expect.objectContaining({ revision: 1 })));
    fireEvent.change(screen.getByLabelText("Review revision"), { target: { value: "2" } });
    fireEvent.click(screen.getByLabelText("Changes since previous"));
    await waitFor(() => expect(hooks.useReview).toHaveBeenCalledWith("rev-x", { revision: 2, diffFrom: 1 }));
    expect(screen.getByText("new paragraph").closest("pre")).toHaveClass("bg-emerald-950");
    expect(screen.getByText("old paragraph").closest("pre")).toHaveClass("bg-red-950");
  });

  it("opens a comment popover from an in-body text selection", async () => {
    renderPane();
    const body = screen.getByText("Visible body");
    const range = document.createRange();
    range.selectNodeContents(body);
    vi.spyOn(window, "getSelection").mockReturnValue({
      rangeCount: 1,
      isCollapsed: false,
      getRangeAt: () => range,
      toString: () => "Visible body",
    } as unknown as Selection);
    fireEvent.mouseUp(body);
    fireEvent.click(await screen.findByRole("button", { name: "Comment" }));
    fireEvent.change(screen.getByLabelText("Comment"), { target: { value: "Please explain." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(hooks.comment.mutateAsync).toHaveBeenCalledWith({
      review_id: "rev-x", revision: 2, quote: "Visible body", heading_path: ["Goal"], body: "Please explain.",
    }));
  });

  it("submits a section comment and decisions with the viewed revision", async () => {
    renderPane();
    fireEvent.click(screen.getByRole("button", { name: "Comment on this section" }));
    fireEvent.change(screen.getByLabelText("Comment"), { target: { value: "Clarify this." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(hooks.comment.mutateAsync).toHaveBeenCalledWith({
      review_id: "rev-x", revision: 2, quote: null, heading_path: ["Goal"], body: "Clarify this.",
    }));
    fireEvent.change(screen.getByLabelText("Decision note"), { target: { value: "Looks good" } });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(hooks.decide.mutateAsync).toHaveBeenCalledWith({
      review_id: "rev-x", revision: 2, decision: "approve", note: "Looks good",
    }));
  });

  it("keeps a draft and its opening revision when the viewed revision changes", async () => {
    renderPane();
    fireEvent.click(screen.getByRole("button", { name: "Comment on this section" }));
    fireEvent.change(screen.getByLabelText("Comment"), { target: { value: "Keep this draft." } });
    fireEvent.change(screen.getByLabelText("Review revision"), { target: { value: "1" } });
    expect(screen.getByLabelText("Comment")).toHaveValue("Keep this draft.");
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(hooks.comment.mutateAsync).toHaveBeenCalledWith({
      review_id: "rev-x", revision: 2, quote: null, heading_path: ["Goal"], body: "Keep this draft.",
    }));
  });

  it("retains the draft on submission failure and allows retry", async () => {
    hooks.comment.mutateAsync.mockRejectedValueOnce(new Error("Comment service unavailable"));
    renderPane();
    fireEvent.click(screen.getByRole("button", { name: "Comment on this section" }));
    fireEvent.change(screen.getByLabelText("Comment"), { target: { value: "Do not lose this." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Comment service unavailable");
    expect(screen.getByLabelText("Comment")).toHaveValue("Do not lose this.");
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(hooks.comment.mutateAsync).toHaveBeenCalledTimes(2);
  });

  it("focuses without scrolling, contains Tab navigation, and restores focus on Escape", () => {
    renderPane();
    const button = screen.getByRole("button", { name: "Comment on this section" });
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    fireEvent.click(button);
    const input = screen.getByLabelText("Comment");
    expect(input).toHaveFocus();
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    fireEvent.keyDown(input, { key: "Tab", shiftKey: true });
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: "Tab" });
    expect(input).toHaveFocus();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(button).toHaveFocus();
  });

  it("submits the selected response route with requested changes", async () => {
    renderPane();
    expect(screen.getByLabelText("Response route")).toHaveTextContent("routed by the project's router (no class hint)");
    expect(screen.queryByLabelText("Revision intelligence class")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
    expect(screen.getByText("Who revises this after your feedback?")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Revision intelligence class"), {
      target: { value: "standard-high" },
    });
    expect(screen.getByLabelText("Response route")).toHaveTextContent("(class hint standard-high)");
    expect(screen.queryByLabelText("Revision profile")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Send changes request" }));
    await waitFor(() => expect(hooks.decide.mutateAsync).toHaveBeenCalledWith({
      review_id: "rev-x", revision: 2, decision: "request_changes",
      responder_class: "standard-high",
    }));
  });

  it("requires a note when rejecting and submits a distinct reject decision", async () => {
    renderPane();
    const reject = screen.getByRole("button", { name: "Reject" });
    expect(reject).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Decision note"), {
      target: { value: "Use the smaller fix approach." },
    });
    fireEvent.click(reject);
    await waitFor(() => expect(hooks.decide.mutateAsync).toHaveBeenCalledWith({
      review_id: "rev-x", revision: 2, decision: "reject", note: "Use the smaller fix approach.",
    }));
  });

  it("shows the route saved on a decided revision beside the decision controls", () => {
    hooks.useReview.mockImplementation(() => ({
      data: {
        ...response,
        response_route: { ...response.response_route,
          summary: "Request changes → new revision task on standard-high (explicit); Approve → sent to the supervisor." },
      },
      isLoading: false,
      error: null,
    }));
    renderPane();
    expect(screen.getByLabelText("Response route")).toHaveTextContent("new revision task on standard-high (explicit)");
  });

  it("keeps approval separate from the revision selector", async () => {
    renderPane();
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
    fireEvent.change(screen.getByLabelText("Revision intelligence class"), {
      target: { value: "standard-high" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(hooks.decide.mutateAsync).toHaveBeenCalledWith({
      review_id: "rev-x", revision: 2, decision: "approve",
    }));
  });

  it("shows the live revision banner", async () => {
    renderPane();
    await act(async () => hooks.listener?.({ event_type: "review.revised", review_id: "rev-x" }));
    expect(screen.getByText("Revised since you opened it — reload")).toBeInTheDocument();
  });

  it("shows the diverged vault banner and imports local edits", async () => {
    hooks.useReview.mockImplementation(() => ({
      data: { ...response, vault_state: "diverged" }, isLoading: false, error: null,
    }));
    renderPane();
    fireEvent.click(screen.getByRole("button", { name: "Import my edits" }));
    await waitFor(() => expect(hooks.importEdits.mutateAsync).toHaveBeenCalledWith({ review_id: "rev-x" }));
    expect(screen.getByText("This file was edited outside the review")).toBeInTheDocument();
  });

  it("explains why decisions are disabled for delegated and stale views", async () => {
    hooks.useReview.mockImplementation(() => ({
      data: { ...response, review: { ...response.review, decider: "user_or_supervisor" } }, isLoading: false, error: null,
    }));
    const { rerender } = renderPane();
    expect(screen.getByText("delegated to supervisor")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    hooks.useReview.mockImplementation((_id: string, opts?: { revision?: number }) => ({
      data: { ...response, revision: { ...response.revision, revision: opts?.revision ?? 2 } }, isLoading: false, error: null,
    }));
    rerender(<MemoryRouter><ReviewPaneView args={{ reviewId: "rev-x" }} close={vi.fn()} setArgs={vi.fn()} setToolbar={vi.fn()} setShortcuts={vi.fn()} /></MemoryRouter>);
    fireEvent.change(screen.getByLabelText("Review revision"), { target: { value: "1" } });
    await waitFor(() => expect(screen.getByText("revised since you opened it — reload")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Request changes" })).toBeDisabled();
  });
});
