import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { testQueryClient } from "../../../testUtils/dashboardState";
import FocusShell from "../FocusShell";
import FocusConversation from "../FocusConversation";
import FocusConversationList from "../FocusConversationList";

const wire = vi.hoisted(() => ({ status: vi.fn(), history: vi.fn() }));

vi.mock("@aq/ts-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@aq/ts-client")>()),
  supervisorInboxStatus: wire.status,
  supervisorInboxHistory: wire.history,
}));

vi.mock("../../chat/ChatConversation", () => ({
  default: (props: { sessionAddress?: string; threadIdOverride?: string }) => (
    <div>
      <output aria-label="Chat session">{props.sessionAddress ?? ""}</output>
      <output aria-label="Chat thread">{props.threadIdOverride ?? ""}</output>
    </div>
  ),
}));

const conversation = {
  id: "conv-1",
  transport: "discord",
  guild_id: "g1",
  channel_id: "c1",
  external_root_message_id: "m1",
  external_thread_id: "t1",
  thread_id: "conversation:conv-1",
  created_by: "human:discord:42",
  audience: [],
  state: "open",
  created_at: 1790000000,
  updated_at: 1790000500,
  closed_at: null,
  inputs: [{ id: "i1", received_at: 1790000400 }],
  next_before: null,
  next_before_id: null,
};

function renderAt(path: string) {
  return render(
    <QueryClientProvider client={testQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/focus" element={<FocusShell />}>
            <Route path="conversations" element={<FocusConversationList />} />
            <Route path="conversations/:conversationId" element={<FocusConversation />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  wire.status.mockResolvedValue({ data: { success: true, enabled: true } });
  wire.history.mockResolvedValue({
    data: { success: true, conversations: [conversation], next_before: null, next_before_id: null },
  });
});

describe("FocusConversationList", () => {
  it("lists conversations and links to the focus detail", async () => {
    renderAt("/focus/conversations");
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("Conversations");
    const row = await screen.findByRole("link", { name: /Discord · 42/ });
    expect(row).toHaveAttribute("href", "/focus/conversations/conv-1");
    expect(screen.getByText("Conversations · 1")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open in full dashboard" })).toHaveAttribute(
      "href",
      "/conversations",
    );
  });

  it("says so when the route is disabled", async () => {
    wire.status.mockResolvedValue({ data: { success: true, enabled: false } });
    renderAt("/focus/conversations");
    expect(await screen.findByText("Discord conversations are disabled.")).toBeInTheDocument();
  });
});

describe("FocusConversation", () => {
  it("renders the transcript for the conversation the URL names", async () => {
    renderAt("/focus/conversations/conv-1");
    expect(await screen.findByLabelText("Chat session")).toHaveTextContent("supervisor-global");
    expect(screen.getByLabelText("Chat thread")).toHaveTextContent("conversation:conv-1");
    expect(screen.getByText("conv-1")).toBeInTheDocument();
    expect(screen.getByText(/discord · open/)).toBeInTheDocument();
    expect(wire.history).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.objectContaining({ conversation_id: "conv-1" }) }),
    );
  });

  it("offers a way back when the id names no conversation", async () => {
    wire.history.mockResolvedValue({
      data: { success: true, conversations: [], next_before: null, next_before_id: null },
    });
    renderAt("/focus/conversations/missing");
    expect(await screen.findByText("No conversation with that id.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "All conversations" })).toHaveAttribute(
      "href",
      "/focus/conversations",
    );
  });

  it("surfaces a failed read instead of an empty transcript", async () => {
    wire.history.mockRejectedValue(new Error("conversation history is unavailable"));
    renderAt("/focus/conversations/conv-1");
    expect(await screen.findByRole("alert")).toHaveTextContent("conversation history is unavailable");
  });
});