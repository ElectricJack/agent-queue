import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchChatMessages, sendChatMessage } from "../../api/chat";
import { useChatTranscript } from "./useChatTranscript";

vi.mock("../../api/chat", () => ({ fetchChatMessages: vi.fn(), sendChatMessage: vi.fn() }));
vi.mock("../../ws/useEventStream", () => ({ useEventStream: vi.fn() }));
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(fetchChatMessages).mockResolvedValue({ success: true, session: "supervisor-global", project_id: "", count: 0, messages: [] });
});

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderHook(({ thread }) => useChatTranscript("", {
    sessionAddress: "supervisor-global", threadIdOverride: thread,
  }), {
    initialProps: { thread: "dashboard:global" },
    wrapper: ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>,
  });
}

describe("useChatTranscript thread changes", () => {
  it("resets pending state and ignores failures from a previous thread", async () => {
    let rejectSend!: (error: Error) => void;
    vi.mocked(sendChatMessage).mockImplementation(() => new Promise((_resolve, reject) => { rejectSend = reject; }));
    const hook = setup();
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    let sending!: Promise<void>;
    act(() => { sending = hook.result.current.send("Old thread input"); });
    expect(hook.result.current.isSending).toBe(true);
    hook.rerender({ thread: "conversation:conv-one" });
    await waitFor(() => expect(hook.result.current.items).toEqual([]));
    expect(hook.result.current.thinking).toBeNull();
    expect(hook.result.current.isSending).toBe(false);
    await act(async () => { rejectSend(new Error("Old thread failure")); await sending; });
    expect(hook.result.current.sendError).toBeNull();
  });

  it("does not let an old completed send clear a new thread's send state", async () => {
    let resolveOld!: (value: Awaited<ReturnType<typeof sendChatMessage>>) => void;
    let resolveNew!: (value: Awaited<ReturnType<typeof sendChatMessage>>) => void;
    vi.mocked(sendChatMessage)
      .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveNew = resolve; }));
    const hook = setup();
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    let oldSend!: Promise<void>;
    act(() => { oldSend = hook.result.current.send("Old"); });
    hook.rerender({ thread: "conversation:conv-one" });
    await waitFor(() => expect(hook.result.current.isLoading).toBe(false));
    let newSend!: Promise<void>;
    act(() => { newSend = hook.result.current.send("New"); });
    await act(async () => { resolveOld({ success: true, message_id: "old", state: "queued" }); await oldSend; });
    expect(hook.result.current.isSending).toBe(true);
    expect(hook.result.current.thinking).not.toBeNull();
    await act(async () => { resolveNew({ success: true, message_id: "new", state: "queued" }); await newSend; });
    expect(hook.result.current.isSending).toBe(false);
  });
});
