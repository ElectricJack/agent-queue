import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import TerminalInputBar from "../TerminalInputBar";
import { encodeEntry, entryText, MAX_ENTRY_BYTES, TERMINAL_KEYS } from "../terminalInput";

afterEach(cleanup);

const decode = (bytes: Uint8Array) => new TextDecoder().decode(bytes);

function renderBar({ connected = true } = {}) {
  const onSubmit = vi.fn();
  const onFocusChange = vi.fn();
  const inputRef = createRef<HTMLTextAreaElement>();
  const view = render(
    <TerminalInputBar name="worker-a" connected={connected} onSubmit={onSubmit} inputRef={inputRef} onFocusChange={onFocusChange} />,
  );
  const box = screen.getByRole("textbox", { name: "worker-a terminal input" }) as HTMLTextAreaElement;
  const send = screen.getByRole("button", { name: "Send to worker-a" });
  return { ...view, onSubmit, onFocusChange, inputRef, box, send };
}

describe("TerminalInputBar", () => {
  it("is a phone line editor: send key hint, no autocorrect, 16 px text so iOS does not zoom", () => {
    const { box, inputRef } = renderBar();
    expect(inputRef.current).toBe(box);
    expect(box).toHaveAttribute("enterkeyhint", "send");
    expect(box).toHaveAttribute("autocapitalize", "off");
    expect(box).toHaveAttribute("autocorrect", "off");
    expect(box).toHaveAttribute("spellcheck", "false");
    expect(box).toHaveClass("text-base");
    expect(box).toHaveAccessibleDescription(/Enter sends.*Shift\+Enter adds a line/);
  });

  it("Enter sends the entry and clears it; Shift+Enter and IME composition do not", () => {
    const { box, onSubmit } = renderBar();
    fireEvent.change(box, { target: { value: "yes, go ahead" } });
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith("yes, go ahead");
    expect(box).toHaveValue("");
  });

  it("the Send button keeps a pasted multi-line entry whole and never takes focus", () => {
    const { box, send, onSubmit } = renderBar();
    expect(send).toBeDisabled();
    fireEvent.change(box, { target: { value: "first line\nsecond line" } });
    expect(send).toBeEnabled();
    // A cancelled mousedown keeps focus (and the on-screen keyboard) on the input.
    expect(fireEvent.mouseDown(send)).toBe(false);
    fireEvent.click(send);
    expect(onSubmit).toHaveBeenCalledExactlyOnceWith("first line\nsecond line");
  });

  it("keeps the draft and refuses to send while the socket is not connected", () => {
    const { box, send, onSubmit } = renderBar({ connected: false });
    expect(box).toBeEnabled();
    expect(box).toHaveAttribute("placeholder", "Connecting…");
    fireEvent.change(box, { target: { value: "draft" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(send).toBeDisabled();
    expect(onSubmit).not.toHaveBeenCalled();
    expect(box).toHaveValue("draft");
  });

  it("refuses an entry over the 64 KiB limit and says so", () => {
    const { box, send, onSubmit } = renderBar();
    fireEvent.change(box, { target: { value: "x".repeat(MAX_ENTRY_BYTES + 1) } });
    expect(screen.getByRole("alert")).toHaveTextContent(/Too long to send/);
    expect(box).toHaveAttribute("aria-invalid", "true");
    expect(send).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("reports focus so the terminal can fit the keyboard viewport", () => {
    const { box, onFocusChange } = renderBar();
    fireEvent.focus(box);
    fireEvent.blur(box);
    expect(onFocusChange.mock.calls).toEqual([[true], [false]]);
  });
});

describe("terminal input encoding", () => {
  it("sends one line as typed and several lines as one bracketed paste, with CR line ends as a terminal paste", () => {
    expect(decode(encodeEntry("ls -la"))).toBe("ls -la");
    expect(decode(encodeEntry("a\nb"))).toBe("\x1b[200~a\rb\x1b[201~");
    expect(decode(encodeEntry("a\r\nb\rc"))).toBe("\x1b[200~a\rb\rc\x1b[201~");
  });

  it("drops control characters so pasted text cannot end the paste early or type keys", () => {
    expect(entryText("safe\x1b[201~\x03rm\ttab\x7f")).toBe("safe[201~rm\ttab");
    expect(decode(encodeEntry("x\x1b[201~\ny"))).toBe("\x1b[200~x[201~\ry\x1b[201~");
  });

  it("key strip bytes: Esc, Tab, Ctrl-C, arrows, Enter and 1-3", () => {
    expect(TERMINAL_KEYS.map((key) => [key.label, key.bytes])).toEqual([
      ["Esc", "\x1b"], ["Tab", "\t"], ["Ctrl-C", "\x03"], ["↑", "\x1b[A"], ["↓", "\x1b[B"],
      ["Enter", "\r"], ["1", "1"], ["2", "2"], ["3", "3"],
    ]);
  });
});
