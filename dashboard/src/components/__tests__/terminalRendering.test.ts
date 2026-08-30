import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Terminal } from "@xterm/xterm";
import { connectTerminal, type TerminalConnection } from "../../ws/terminalSocket";
import { TerminalSocketMock } from "../../testUtils/terminal";

let terminal: Terminal;
let connection: TerminalConnection;
beforeEach(() => {
  // This exercises the real parser without mounting a canvas/DOM renderer.
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  TerminalSocketMock.instances = [];
  vi.stubGlobal("WebSocket", TerminalSocketMock);
});
afterEach(() => { connection?.close(); terminal?.dispose(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("Real xterm rendering of the terminal byte stream", () => {
  it("retains RGB, indexed backgrounds, reverse video, cursor positioning, and split UTF-8", async () => {
    const { Terminal } = await import("@xterm/xterm");
    terminal = new Terminal({ cols: 80, rows: 24, logLevel: "off" });
    const renders: Promise<void>[] = [];
    connection = connectTerminal({
      sessionId: "color-test", cols: 80, rows: 24, onState: () => {},
      write: (bytes, processed) => {
        renders.push(new Promise((resolve) => terminal.write(bytes, () => { processed(); resolve(); })));
      },
    });
    const socket = TerminalSocketMock.instances[0]!;
    socket.ready();
    const encoder = new TextEncoder();
    socket.message(encoder.encode("\x1b[38;2;255;90;0m\x1b[48;5;32m\x1b[7mC\x1b[0m"));
    socket.message(Uint8Array.from([0xe4]));
    socket.message(Uint8Array.from([0xb8, 0x96]));
    socket.message(encoder.encode("\x1b[2;3HPOSITION"));
    await Promise.all(renders);
    const colored = terminal.buffer.active.getLine(0)!.getCell(0)!;
    expect(colored.getChars()).toBe("C");
    expect(colored.isFgRGB()).toBeTruthy();
    expect(colored.getFgColor()).toBe(0xff5a00);
    expect(colored.isBgPalette()).toBeTruthy();
    expect(colored.getBgColor()).toBe(32);
    expect(colored.isInverse()).toBeTruthy();
    expect(terminal.buffer.active.getLine(0)!.getCell(1)!.getChars()).toBe("世");
    expect(terminal.buffer.active.getLine(1)!.translateToString(true)).toBe("  POSITION");
    expect(socket.controls()).toHaveLength(4);
  });
});
