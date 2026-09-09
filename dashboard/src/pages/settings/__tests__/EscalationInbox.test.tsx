import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import EscalationInbox from "../EscalationInbox";

const api = vi.hoisted(() => ({
  escalations: [] as Array<Record<string, unknown>>,
  detail: undefined as Record<string, unknown> | undefined,
  reply: vi.fn(),
  selected: null as string | null,
}));

vi.mock("../../../api/messaging", () => ({
  useEscalations: () => ({ data: { escalations: api.escalations }, isLoading: false, error: null }),
  useEscalation: (id: string | null) => {
    api.selected = id;
    return { data: id ? api.detail : undefined };
  },
  useEscalationReply: () => ({ mutateAsync: api.reply, isPending: false }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  api.escalations = [
    {
      id: "esc-1",
      project_id: "agent-queue",
      task_id: "noble-ridge.4",
      state: "needs_human",
      severity: "high",
      summary: "Integration branch will not build",
      investigation: "Ran the suite twice; the failure predates the branch.",
      decision_requested: "Revert the dependency bump or pin it?",
      supervisor_owner: "supervisor-agent-queue",
      updated_at: 1_788_000_000,
      pending_delivery: true,
    },
    {
      id: "esc-2",
      project_id: "other",
      task_id: null,
      state: "resolved",
      severity: "low",
      summary: "Old incident",
      investigation: "",
      decision_requested: "",
      supervisor_owner: "supervisor-other",
      updated_at: 1_787_000_000,
      pending_delivery: false,
    },
  ];
  api.detail = {
    messages: [
      { id: "m1", direction: "outbound", verified_actor: "supervisor-agent-queue", text: "Which should I do?" },
    ],
    deliveries: [{ id: "d1", kind: "root", status: "unknown" }],
  };
  api.reply.mockResolvedValue({ success: true });
});

afterEach(cleanup);

function renderInbox() {
  return render(
    <MemoryRouter>
      <EscalationInbox />
    </MemoryRouter>,
  );
}

describe("Escalation inbox", () => {
  it("lists escalations with state, project, open count and pending delivery", () => {
    renderInbox();
    expect(screen.getByText(/1 open of 2\./)).toBeInTheDocument();
    expect(screen.getByText("Integration branch will not build")).toBeInTheDocument();
    expect(screen.getByText("delivery pending")).toBeInTheDocument();
    expect(screen.getByText("needs_human")).toBeInTheDocument();
  });

  it("shows conversation, supervisor ownership, unconfirmed delivery and a task link on open", async () => {
    renderInbox();
    fireEvent.click(screen.getByText("Integration branch will not build"));
    await waitFor(() => expect(screen.getByText("supervisor-agent-queue")).toBeInTheDocument());
    expect(screen.getByRole("link", { name: "noble-ridge.4" })).toHaveAttribute("href", "/tasks/noble-ridge.4");
    expect(screen.getByText(/Revert the dependency bump or pin it\?/)).toBeInTheDocument();
    expect(screen.getByText(/External delivery not confirmed: root unknown/)).toBeInTheDocument();
    expect(screen.getByText("Which should I do?")).toBeInTheDocument();
  });

  it("sends a human reply through escalation_reply with a unique external message id", async () => {
    renderInbox();
    fireEvent.click(screen.getByText("Integration branch will not build"));
    const box = await screen.findByLabelText("Reply");
    fireEvent.change(box, { target: { value: "  Pin it to 2.4.1  " } });
    fireEvent.click(screen.getByRole("button", { name: "Send to supervisor" }));

    await waitFor(() => expect(api.reply).toHaveBeenCalledTimes(1));
    const payload = api.reply.mock.calls[0]![0] as Record<string, string>;
    expect(payload.escalation_id).toBe("esc-1");
    expect(payload.text).toBe("Pin it to 2.4.1");
    expect(payload.external_message_id).toMatch(/^dashboard:esc-1:\d+$/);
  });

  it("does not send an empty reply", async () => {
    renderInbox();
    fireEvent.click(screen.getByText("Integration branch will not build"));
    const button = await screen.findByRole("button", { name: "Send to supervisor" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(api.reply).not.toHaveBeenCalled();
  });
});
