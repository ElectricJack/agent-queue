import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { testQueryClient } from "../../../testUtils/dashboardState";
import FocusShell from "../FocusShell";
import FocusEscalation from "../FocusEscalation";

const wire = vi.hoisted(() => ({ get: vi.fn(), reply: vi.fn() }));

vi.mock("../../../api/messaging", () => ({
  useEscalation: vi.fn(() => wire.get()),
  useEscalationReply: vi.fn(() => ({ mutateAsync: wire.reply, isPending: false })),
}));

import { useEscalation } from "../../../api/messaging";

const escalation = {
  id: "escalation-abc",
  project_id: "agent-queue",
  task_id: "nimble-torrent-66",
  task_title: "Opus 5.5 trial",
  source_kind: "question",
  source_identity: "q1",
  incident_key: "k",
  supervisor_owner: "session:supervisor-agent-queue",
  summary: "Keep the Opus trial or revert?",
  investigation: "Ran the trial for two days.",
  decision_requested: "Keep the Opus 5.5 trial or revert to Sonnet?",
  choices: ["Keep Opus", "Switch to Sonnet"],
  severity: "high",
  state: "needs_human",
  revision: 3,
  terminal_outcome: null,
  created_at: 1790000000,
  updated_at: 1790000100,
};

function detail(overrides: Record<string, unknown> = {}) {
  return {
    data: {
      escalation: { ...escalation, ...overrides },
      messages: [
        {
          id: "m1",
          escalation_id: "escalation-abc",
          direction: "inbound",
          transport: "discord",
          verified_actor: "discord:42",
          text: "Switch to Sonnet",
          received_at: 1790000200,
          created_at: 1790000200,
        },
      ],
      deliveries: [],
      actions: [],
    },
    isError: false,
    error: null,
    refetch: vi.fn(),
  };
}

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/focus" element={<FocusShell />}>
            <Route path="escalations/:escalationId" element={<FocusEscalation />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  wire.get.mockReturnValue(detail());
  wire.reply.mockResolvedValue({ success: true });
});

describe("FocusEscalation", () => {
  it("shows the decision, its context, the options and the task link inside focus", async () => {
    renderAt("/focus/escalations/escalation-abc");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(
      "Keep the Opus 5.5 trial or revert to Sonnet?",
    );
    expect(screen.getByText("Keep the Opus trial or revert?")).toBeInTheDocument();
    expect(screen.getByText(/Ran the trial for two days/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep Opus" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Switch to Sonnet" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Task Opus 5.5 trial/ })).toHaveAttribute(
      "href",
      "/focus/tasks/nimble-torrent-66",
    );
    expect(screen.getByText(/inbound · discord:42/)).toBeInTheDocument();
    expect(useEscalation).toHaveBeenCalledWith("escalation-abc");
    // The one address an escalation has: never the settings page (spec §5.4).
    expect(screen.queryByRole("link", { name: "Open in full dashboard" })).not.toBeInTheDocument();
  });

  it("answers a choice with escalation_reply", async () => {
    renderAt("/focus/escalations/escalation-abc");
    await userEvent.click(await screen.findByRole("button", { name: "Keep Opus" }));
    expect(wire.reply).toHaveBeenCalledWith({
      escalation_id: "escalation-abc",
      text: "Keep Opus",
      external_message_id: expect.stringContaining("dashboard:escalation-abc:"),
    });
  });

  it("sends the reply box text and reports a refusal in place", async () => {
    wire.reply.mockRejectedValueOnce(new Error("escalation belongs to another project"));
    renderAt("/focus/escalations/escalation-abc");
    await userEvent.type(await screen.findByLabelText("Reply"), "keep the trial");
    await userEvent.click(screen.getByRole("button", { name: "Send to supervisor" }));
    expect(wire.reply).toHaveBeenCalledWith(expect.objectContaining({ text: "keep the trial" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("escalation belongs to another project");
  });

  it("collapses a terminal escalation to its outcome: no options, no reply box", async () => {
    wire.get.mockReturnValue(detail({ state: "resolved", terminal_outcome: "kept the Opus trial" }));
    renderAt("/focus/escalations/escalation-abc");
    expect(await screen.findByText(/Resolved: kept the Opus trial/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Keep Opus" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Reply")).not.toBeInTheDocument();
  });

  it("offers Retry when the read fails", async () => {
    const refetch = vi.fn();
    wire.get.mockReturnValue({ data: undefined, isError: true, error: new Error("escalation not found"), refetch });
    renderAt("/focus/escalations/missing");
    expect(await screen.findByRole("alert")).toHaveTextContent("escalation not found");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalled();
  });
});