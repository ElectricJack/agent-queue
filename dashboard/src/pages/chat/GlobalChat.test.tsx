import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { supervisorInboxHistory } from "../../api/client";
import { fetchChatMessages, sendChatMessage, type ChatMessagesResponse } from "../../api/chat";
import type { MessageModel } from "../../api/client";
import GlobalChat from "../GlobalChat";
import { historyConversation, historyResult } from "./historyFixtures";

vi.mock("../../api/client", () => ({ supervisorInboxHistory: vi.fn() }));
vi.mock("../../api/chat", () => ({ fetchChatMessages: vi.fn(), sendChatMessage: vi.fn() }));
vi.mock("../../ws/useEventStream", () => ({ useEventStream: vi.fn() }));

beforeAll(() => Object.defineProperty(HTMLElement.prototype, "scrollTo", {
  configurable: true, writable: true, value: vi.fn(),
}));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(supervisorInboxHistory).mockResolvedValue(historyResult([historyConversation()]));
  vi.mocked(fetchChatMessages).mockImplementation(async (_project, opts) => ({
    success: true, session: "supervisor-global", project_id: "", count: 1,
    messages: [{
      id: opts?.threadId, body: opts?.threadId === "dashboard:global" ? "Dashboard history" : "Discord history",
      from_kind: "user", from_id: opts?.threadId === "dashboard:global" ? "dashboard" : "discord:111",
      thread_id: opts?.threadId, created_at: 100,
    } as MessageModel],
  }));
  vi.mocked(sendChatMessage).mockResolvedValue({ success: true, message_id: "sent-one", state: "queued" });
});

function Location() {
  return <output aria-label="Location">{useLocation().search}</output>;
}
function renderChat(url = "/?keep=1") {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={[url]}><GlobalChat /><Location /></MemoryRouter>
  </QueryClientProvider>);
}
function choose(thread: string) {
  fireEvent.change(screen.getByRole("combobox", { name: "Conversation" }), { target: { value: thread } });
}

describe("GlobalChat conversation filtering", () => {
  it("filters history, uses dashboard identity to send, and restores All", async () => {
    renderChat();
    await screen.findByText("Dashboard history");
    await screen.findByRole("option", { name: /Discord · 111/ });
    choose("conversation:conv-one");
    await screen.findByText("Discord history");
    expect(screen.queryByText("Dashboard history")).not.toBeInTheDocument();
    expect(screen.getByText("Discord · 111 (verified)")).toBeInTheDocument();
    expect(fetchChatMessages).toHaveBeenLastCalledWith("", {
      threadId: "conversation:conv-one", limit: 200, sessionAddress: "supervisor-global",
    });
    expect(screen.getByLabelText("Location")).toHaveTextContent("?keep=1&conversation=conv-one");
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "From dashboard" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(sendChatMessage).toHaveBeenCalledWith("", "From dashboard", {
      threadId: "conversation:conv-one", sessionAddress: "supervisor-global",
    }));
    expect(screen.getByText("You")).toBeInTheDocument();
    choose("dashboard:global");
    await screen.findByText("Dashboard history");
    expect(screen.queryByText("Discord history")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Location")).toHaveTextContent("?keep=1");
  });

  it("does not carry a pending send or late failure into the next conversation", async () => {
    let rejectSend!: (error: Error) => void;
    vi.mocked(sendChatMessage).mockImplementation(() => new Promise((_resolve, reject) => { rejectSend = reject; }));
    renderChat();
    await screen.findByText("Dashboard history");
    await screen.findByRole("option", { name: /Discord · 111/ });
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Still sending" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(screen.getByText("Still sending", { selector: "div" })).toBeInTheDocument();
    choose("conversation:conv-one");
    await screen.findByText("Discord history");
    rejectSend(new Error("Old send failed"));
    await waitFor(() => expect(screen.queryByText("Still sending")).not.toBeInTheDocument());
    expect(screen.queryByText("Old send failed")).not.toBeInTheDocument();
    expect(screen.queryByText("sending…")).not.toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("");
  });

  it("loads the selected conversation from a deep link", async () => {
    renderChat("/?conversation=conv-one");
    await screen.findByText("Discord history");
    expect(fetchChatMessages).toHaveBeenCalledWith("", {
      threadId: "conversation:conv-one", limit: 200, sessionAddress: "supervisor-global",
    });
  });

  it("does not show an old conversation while the new history is loading", async () => {
    const never = new Promise<ChatMessagesResponse>(() => {});
    vi.mocked(fetchChatMessages).mockResolvedValueOnce({
      success: true, session: "supervisor-global", project_id: "", count: 1,
      messages: [{ id: "first", from_kind: "user", from_id: "dashboard", body: "Dashboard history" } as MessageModel],
    }).mockReturnValueOnce(never);
    renderChat();
    await screen.findByText("Dashboard history");
    await screen.findByRole("option", { name: /Discord · 111/ });
    choose("conversation:conv-one");
    expect(await screen.findByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("Dashboard history")).not.toBeInTheDocument();
  });
});
