import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Terminal } from "@xterm/xterm";
import { swallowAltScreen } from "../phoneTerminalStream";

let terminal: Terminal;
beforeEach(() => {
  // The real parser and buffers, without mounting a canvas/DOM renderer.
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});
afterEach(() => { terminal?.dispose(); vi.restoreAllMocks(); });

async function create(rows: number, phone: boolean) {
  const { Terminal } = await import("@xterm/xterm");
  terminal = new Terminal({ cols: 20, rows, scrollback: 100, logLevel: "off" });
  if (phone) swallowAltScreen(terminal);
  return (data: string) => new Promise<void>((resolve) => terminal.write(data, resolve));
}
function lines() {
  const buffer = terminal.buffer.active;
  return Array.from({ length: buffer.length }, (_, row) => buffer.getLine(row)!.translateToString(true));
}

describe("Phone terminal scrollback (real xterm.js)", () => {
  it("keeps tmux in the normal buffer: smcup and rmcup are swallowed, other modes still apply", async () => {
    const write = await create(4, true);
    await write("\x1b[?1049h\x1b[?2004h");
    expect(terminal.buffer.active.type).toBe("normal");
    expect(terminal.modes.bracketedPasteMode).toBe(true);
    await write("\x1b[?1049l\x1b[?47h\x1b[?1047h");
    expect(terminal.buffer.active.type).toBe("normal");
  });

  it("without the phone's handling, smcup leaves no scrollback at all", async () => {
    const write = await create(4, false);
    await write("\x1b[?1049h\x1b[H\x1b[2Jone\r\ntwo\r\nthree\r\nfour\r\nfive");
    expect(terminal.buffer.active.type).toBe("alternate");
    expect(lines()).not.toContain("one");
  });

  it("tmux's line feeds above its status line move lines into scrollback", async () => {
    const write = await create(4, true);
    await write("\x1b[?1049h\x1b[H\x1b[2J\x1b[1;3ra\r\nb\r\nc\x1b[4;1HSTATUS\x1b[3;1H\n\nd");
    expect(lines()).toEqual(["a", "b", "c", "", "d", "STATUS"]);
    expect(terminal.buffer.active.baseY).toBe(2);
  });

  it("CSI n S drops the lines it scrolls (why a phone attach makes tmux use line feeds)", async () => {
    const write = await create(4, true);
    await write("one\r\ntwo\r\nthree\r\nfour\x1b[2S");
    expect(terminal.buffer.active.length).toBe(4);
    expect(lines()).not.toContain("one");
  });

  it("the server's history prefill ends up above tmux's first screen", async () => {
    const write = await create(3, true);
    // PtyTmuxClient.history framing: lines joined with SGR resets, then one
    // line feed per row so the last line sits just above a blank screen.
    await write("\x1b[32mold-1\x1b[0m\r\nold-2\x1b[0m" + "\r\n".repeat(3));
    await write("\x1b[?1049h\x1b[H\x1b[2Jlive-1\r\nlive-2\r\n$ ");
    expect(lines()).toEqual(["old-1", "old-2", "live-1", "live-2", "$ "]);
    expect(terminal.buffer.active.baseY).toBe(2);
    expect(terminal.buffer.active.getLine(0)!.getCell(0)!.getFgColor()).toBe(2);
  });
});
