import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { supervisorInboxHistory } from "../../api/client";
import ConversationPicker from "./ConversationPicker";
import { historyConversation, historyInput, historyResult } from "./historyFixtures";

vi.mock("../../api/client", () => ({ supervisorInboxHistory: vi.fn() }));

function renderPicker(onSelect = vi.fn()) {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ConversationPicker threadId="dashboard:global" onSelect={onSelect} />
    </QueryClientProvider>,
  );
  return onSelect;
}

beforeEach(() => vi.clearAllMocks());

describe("ConversationPicker", () => {
  it("lists state, creator and last input time and selects the conversation thread", async () => {
    vi.mocked(supervisorInboxHistory).mockResolvedValue(historyResult([
      historyConversation({ inputs: [historyInput(200), historyInput(150)] }),
    ]));
    const select = renderPicker();
    const option = await screen.findByRole("option", { name: /Discord · 111.*open/ });
    expect(option).toHaveTextContent(new Date(200_000).toLocaleString());
    fireEvent.change(screen.getByRole("combobox", { name: "Conversation" }), { target: { value: "conversation:conv-one" } });
    expect(select).toHaveBeenCalledWith("conversation:conv-one");
    fireEvent.change(screen.getByRole("combobox", { name: "Conversation" }), { target: { value: "dashboard:global" } });
    expect(select).toHaveBeenLastCalledWith("dashboard:global");
  });

  it("loads older conversations using the history cursor", async () => {
    vi.mocked(supervisorInboxHistory)
      .mockResolvedValueOnce(historyResult([], 100, "conv-tied"))
      .mockResolvedValueOnce(historyResult());
    renderPicker();
    fireEvent.click(await screen.findByRole("button", { name: "Load older conversations" }));
    await waitFor(() => expect(supervisorInboxHistory).toHaveBeenLastCalledWith({
      body: { limit: 50, before: 100, before_id: "conv-tied" },
    }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Load older conversations" })).not.toBeInTheDocument());
  });

  it("pages by time alone when the cursor carries no tie-break id", async () => {
    vi.mocked(supervisorInboxHistory)
      .mockResolvedValueOnce(historyResult([], 100))
      .mockResolvedValueOnce(historyResult());
    renderPicker();
    fireEvent.click(await screen.findByRole("button", { name: "Load older conversations" }));
    await waitFor(() => expect(supervisorInboxHistory).toHaveBeenLastCalledWith({ body: { limit: 50, before: 100 } }));
  });

  it("shows errors while keeping the dashboard conversation available", async () => {
    vi.mocked(supervisorInboxHistory).mockRejectedValue(new Error("API 503: unavailable"));
    renderPicker();
    expect(await screen.findByRole("alert")).toHaveTextContent("API 503: unavailable");
    expect(screen.getByRole("option", { name: "All" })).toBeInTheDocument();
  });
});
