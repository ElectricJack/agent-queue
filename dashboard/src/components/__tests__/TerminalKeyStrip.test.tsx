import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import TerminalKeyStrip from "../TerminalKeyStrip";

afterEach(cleanup);

describe("TerminalKeyStrip", () => {
  it("offers the keys a phone lacks, in order, as 44 px primary controls in one sideways row", () => {
    render(<TerminalKeyStrip name="worker-a" disabled={false} onKey={vi.fn()} />);
    const strip = screen.getByRole("toolbar", { name: "worker-a terminal keys" });
    expect(strip).toHaveAttribute("data-allow-overflow-x");
    expect(strip).toHaveClass("overflow-x-auto");
    const keys = within(strip).getAllByRole("button");
    expect(keys.map((key) => key.getAttribute("aria-label"))).toEqual([
      "Send Escape", "Send Tab", "Send Ctrl-C", "Send Up arrow", "Send Down arrow",
      "Send Enter", "Send 1", "Send 2", "Send 3",
    ]);
    expect(keys.map((key) => key.textContent)).toEqual(["Esc", "Tab", "Ctrl-C", "↑", "↓", "Enter", "1", "2", "3"]);
    for (const key of keys) expect(key).toHaveAttribute("data-primary-control");
  });

  it("a tap sends the key's bytes without taking focus from the input bar", () => {
    const onKey = vi.fn();
    render(
      <>
        <textarea aria-label="input" />
        <TerminalKeyStrip name="worker-a" disabled={false} onKey={onKey} />
      </>,
    );
    const input = screen.getByRole("textbox", { name: "input" });
    input.focus();
    for (const name of ["Send Escape", "Send Ctrl-C", "Send Up arrow", "Send 2"]) {
      const key = screen.getByRole("button", { name });
      // A cancelled mousedown is what keeps the on-screen keyboard up.
      expect(fireEvent.mouseDown(key)).toBe(false);
      fireEvent.click(key);
    }
    expect(onKey.mock.calls.map(([key]) => key.bytes)).toEqual(["\x1b", "\x03", "\x1b[A", "2"]);
    expect(input).toHaveFocus();
  });

  it("is disabled while the keyboard socket is not connected", () => {
    const onKey = vi.fn();
    render(<TerminalKeyStrip name="worker-a" disabled onKey={onKey} />);
    for (const key of screen.getAllByRole("button")) {
      expect(key).toBeDisabled();
      fireEvent.click(key);
    }
    expect(onKey).not.toHaveBeenCalled();
  });
});
