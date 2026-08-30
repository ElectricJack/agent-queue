import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { useTerminalInput } from "../useTerminalInput";

const api = vi.hoisted(() => ({ sessionInput: vi.fn() }));
vi.mock("../client", () => api);

function deferred() {
  let resolve!: (value: unknown) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const accepted = { data: { success: true, session_id: "s1", accepted: true } };

afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("Direct terminal input queue", () => {
  it("serializes writes to the same session while allowing another session to proceed", async () => {
    const first = deferred();
    api.sessionInput.mockResolvedValue(accepted).mockReturnValueOnce(first.promise);
    const { result } = renderHook(() => ({
      one: useTerminalInput("s1", true),
      same: useTerminalInput("s1", true),
      other: useTerminalInput("s2", true),
    }));
    act(() => {
      result.current.one.write({ text: "hello" });
      result.current.same.write({ key: "Enter" });
      result.current.other.write({ text: "other" });
    });
    await waitFor(() => expect(api.sessionInput).toHaveBeenCalledTimes(2));
    expect(api.sessionInput.mock.calls.map(([args]) => args.body)).toEqual([
      { session_id: "s1", text: "hello" }, { session_id: "s2", text: "other" },
    ]);
    await act(async () => { first.resolve(accepted); });
    await waitFor(() => expect(api.sessionInput).toHaveBeenCalledTimes(3));
    expect(api.sessionInput).toHaveBeenLastCalledWith({ body: { session_id: "s1", key: "Enter" }, throwOnError: true });
  });

  it.each(["unmount", "session change", "disabled"])("discards unsent input after %s", async (change) => {
    const first = deferred();
    api.sessionInput.mockResolvedValue(accepted).mockReturnValueOnce(first.promise);
    const view = renderHook(({ id, enabled }) => useTerminalInput(id, enabled), {
      initialProps: { id: "s1", enabled: true },
    });
    act(() => {
      view.result.current.write({ text: "already in flight" });
      view.result.current.write({ key: "Enter" });
    });
    await waitFor(() => expect(api.sessionInput).toHaveBeenCalledTimes(1));
    if (change === "unmount") view.unmount();
    else view.rerender({ id: change === "session change" ? "s2" : "s1", enabled: change !== "disabled" });
    await act(async () => { first.resolve(accepted); });
    expect(api.sessionInput).toHaveBeenCalledTimes(1);
    if (change === "session change") {
      act(() => view.result.current.write({ text: "new session" }));
      await waitFor(() => expect(api.sessionInput).toHaveBeenCalledTimes(2));
      expect(api.sessionInput).toHaveBeenLastCalledWith({ body: { session_id: "s2", text: "new session" }, throwOnError: true });
    }
  });

  it("never retries a failed write or sends a queued Enter, even after explicit re-enable", async () => {
    api.sessionInput.mockResolvedValue(accepted).mockRejectedValueOnce(new Error("Terminal input failed"));
    const { result } = renderHook(() => useTerminalInput("s1", true));
    act(() => {
      result.current.write({ text: "failed text" });
      result.current.write({ key: "Enter" });
    });
    await waitFor(() => expect(result.current.error).toContain("Terminal input failed"));
    act(() => result.current.write({ key: "Enter" }));
    expect(api.sessionInput).toHaveBeenCalledTimes(1);
    act(() => {
      result.current.resume();
      result.current.write({ text: "fresh input" });
    });
    await waitFor(() => expect(api.sessionInput).toHaveBeenCalledTimes(2));
    expect(api.sessionInput).toHaveBeenLastCalledWith({ body: { session_id: "s1", text: "fresh input" }, throwOnError: true });
  });

  it("rejects a paste larger than 64 KiB without sending a partial chunk", async () => {
    const { result } = renderHook(() => useTerminalInput("s1", true));
    act(() => result.current.write({ text: "é".repeat(32769) }));
    expect(result.current.error).toMatch(/64 KiB/i);
    expect(api.sessionInput).not.toHaveBeenCalled();
  });
});
