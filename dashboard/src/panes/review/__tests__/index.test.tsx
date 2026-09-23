import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import ReviewPaneView from "../index";

const hooks = vi.hoisted(() => ({
  useReview: vi.fn(),
  comment: { mutateAsync: vi.fn().mockResolvedValue({}) },
  decide: { mutateAsync: vi.fn().mockResolvedValue({}), isPending: false },
  importEdits: { mutateAsync: vi.fn().mockResolvedValue({}), isPending: false },
  listener: null as ((event: { event_type: string; review_id?: string }) => void) | null,
}));

vi.mock("../../../api/reviews", () => ({
  useReview: hooks.useReview,
  useCommentReview: () => hooks.comment,
  useDecideReview: () => hooks.decide,
  useImportReviewEdits: () => hooks.importEdits,
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
    summary: "Request changes → new revision task on fast-high (project_default); Approve → sent to the supervisor.",
    class_summaries: {
      "standard-high": "Request changes → new revision task on standard-high (explicit); Approve → sent to the supervisor.",
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
  it("renders the document title, metadata, TOC, and markdown body", () => {
    renderPane();
    expect(screen.getByRole("heading", { name: "Review title" })).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Goal" })).toBeInTheDocument();
    expect(screen.getByText("Visible body")).toBeInTheDocument();
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

  it("submits the selected response route with requested changes", async () => {
    renderPane();
    expect(screen.getByLabelText("Response route")).toHaveTextContent("new revision task on fast-high (project_default)");
    expect(screen.queryByLabelText("Revision intelligence class")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));
    expect(screen.getByText("Who revises this after your feedback?")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Revision intelligence class"), {
      target: { value: "standard-high" },
    });
    expect(screen.getByLabelText("Response route")).toHaveTextContent("new revision task on standard-high (explicit)");
    fireEvent.change(screen.getByLabelText("Revision profile"), {
      target: { value: "standard-high-codex" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send changes request" }));
    await waitFor(() => expect(hooks.decide.mutateAsync).toHaveBeenCalledWith({
      review_id: "rev-x", revision: 2, decision: "request_changes",
      responder_class: "standard-high", responder_profile: "standard-high-codex",
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
