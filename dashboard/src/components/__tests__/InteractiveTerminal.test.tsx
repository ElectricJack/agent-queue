import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import InteractiveTerminal from "../InteractiveTerminal";

const api = vi.hoisted(() => ({ sessionInput: vi.fn() }));
vi.mock("../../api/client", () => api);

beforeEach(() => { api.sessionInput.mockResolvedValue({ data: { success: true, session_id: "session-b", accepted: true } }); });
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.restoreAllMocks(); });

function terminal(status: "open" | "connecting" | "stopped" | "error" = "open") {
  render(<InteractiveTerminal name="Builder" sessionId="session-b" screen="LIVE TERMINAL SCREEN" status={status} />);
  return screen.getByRole("textbox", { name: "Builder terminal input" });
}

describe("Interactive live terminal", () => {
  it("types directly into the viewed session and sends Enter separately", async () => {
    const input = terminal();
    fireEvent.click(input);
    expect(input).toHaveFocus();
    expect(screen.getByText("LIVE TERMINAL SCREEN")).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "h", code: "KeyH" });
    fireEvent.keyDown(input, { key: "i", code: "KeyI" });
    fireEvent.keyDown(input, { key: "Enter", code: "Enter" });
    await waitFor(() => expect(api.sessionInput).toHaveBeenCalledTimes(3));
    expect(api.sessionInput.mock.calls.map(([args]) => args.body)).toEqual([
      { session_id: "session-b", text: "h" }, { session_id: "session-b", text: "i" }, { session_id: "session-b", key: "Enter" },
    ]);
  });

  it("pastes multiline Unicode literally without adding Enter", async () => {
    const input = terminal();
    fireEvent.paste(input, { clipboardData: { getData: () => "hello\n世界\n" } });
    await waitFor(() => expect(api.sessionInput).toHaveBeenCalledTimes(1));
    expect(api.sessionInput).toHaveBeenCalledWith({ body: { session_id: "session-b", text: "hello\n世界\n" }, throwOnError: true });
  });

  it("supports editing, arrows, newline, completion, and Ctrl+C interrupt", async () => {
    const input = terminal();
    fireEvent.keyDown(input, { key: "Backspace" });
    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "Tab", shiftKey: true });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(input, { key: "c", ctrlKey: true });
    await waitFor(() => expect(api.sessionInput).toHaveBeenCalledTimes(5));
    expect(api.sessionInput.mock.calls.map(([args]) => args.body.key)).toEqual(["BSpace", "Up", "BTab", "C-j", "C-c"]);
  });

  it("allows releasing keyboard focus without sending terminal input", async () => {
    const input = terminal();
    fireEvent.click(input);
    fireEvent.keyDown(input, { key: "m", ctrlKey: true });
    expect(screen.getByRole("button", { name: "Focus Builder terminal" })).toHaveFocus();
    expect(api.sessionInput).not.toHaveBeenCalled();
  });

  it.each(["connecting", "stopped", "error"] as const)("disables input while the pane is %s", (status) => {
    const input = terminal(status);
    expect(input).toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.paste(input, { clipboardData: { getData: () => "ignored" } });
    expect(screen.getByRole("button", { name: "Interrupt Builder" })).toBeDisabled();
    expect(api.sessionInput).not.toHaveBeenCalled();
  });

  it.each(["open", "stopped"] as const)("keeps terminal keystrokes away from navigation shortcuts while %s", async (status) => {
    const navigate = vi.fn();
    render(<div onKeyDown={navigate}>
      <InteractiveTerminal name="Builder" sessionId="session-b" screen="SCREEN" status={status} />
    </div>);
    const input = screen.getByRole("textbox", { name: "Builder terminal input" });
    fireEvent.keyDown(input, { key: "g", code: "KeyG" });
    if (status === "open") await waitFor(() => expect(api.sessionInput).toHaveBeenCalledTimes(1));
    else expect(api.sessionInput).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("preserves copy for selected output and offers an explicit interrupt button", async () => {
    vi.spyOn(window, "getSelection").mockReturnValue({ toString: () => "selected output" } as Selection);
    const input = terminal();
    fireEvent.keyDown(input, { key: "c", ctrlKey: true });
    expect(api.sessionInput).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Interrupt Builder" }));
    await waitFor(() => expect(api.sessionInput).toHaveBeenCalledWith({
      body: { session_id: "session-b", key: "C-c" }, throwOnError: true,
    }));
  });

  it("shows a write error and requires explicit re-enable without replaying input", async () => {
    api.sessionInput.mockRejectedValueOnce(new Error("Terminal input failed"));
    const input = terminal();
    fireEvent.keyDown(input, { key: "x" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(await screen.findByRole("alert")).toHaveTextContent(/Terminal input failed/);
    expect(input).toHaveAttribute("aria-disabled", "true");
    expect(api.sessionInput).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Enable input" }));
    expect(input).toHaveAttribute("aria-disabled", "false");
    fireEvent.keyDown(input, { key: "y" });
    await waitFor(() => expect(api.sessionInput).toHaveBeenCalledTimes(2));
    expect(api.sessionInput).toHaveBeenLastCalledWith({ body: { session_id: "session-b", text: "y" }, throwOnError: true });
  });
});
