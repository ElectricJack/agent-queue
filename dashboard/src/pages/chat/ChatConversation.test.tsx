import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";
import ChatConversation from "./ChatConversation";

vi.mock("./useChatTranscript", () => ({ useChatTranscript: () => ({
  items: [{ kind: "message", msg: {
    id: "input-one", from_kind: "user", from_id: "discord:111111111111111111",
    body_kind: "conversation_input", body: "Hello from Discord", created_at: 100,
  } }], isLoading: false, error: null, send: vi.fn(), isSending: false,
  sendError: null, thinking: null,
}) }));

beforeAll(() => Object.defineProperty(HTMLElement.prototype, "scrollTo", {
  configurable: true, writable: true, value: vi.fn(),
}));

describe("ChatConversation", () => {
  it("shows the verified Discord sender on an input", () => {
    render(<MemoryRouter><ChatConversation projectId="" sessionAddress="supervisor-global" /></MemoryRouter>);
    expect(screen.getByText("Discord · 111111111111111111 (verified)")).toBeInTheDocument();
    expect(screen.getByText("Hello from Discord")).toBeInTheDocument();
  });
});
