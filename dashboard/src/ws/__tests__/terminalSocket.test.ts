import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { connectTerminal, type TerminalConnection, type TerminalConnectionState } from "../terminalSocket";
import { TerminalSocketMock } from "../../testUtils/terminal";

const encoder = new TextEncoder();
let connections: TerminalConnection[] = [];

beforeEach(() => {
  TerminalSocketMock.instances = [];
  vi.stubGlobal("WebSocket", TerminalSocketMock);
});
afterEach(() => {
  connections.forEach((connection) => connection.close());
  connections = [];
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

function connect(sessionId = "session-b") {
  const states: TerminalConnectionState[] = [];
  const writes: { bytes: Uint8Array; processed: () => void }[] = [];
  const connection = connectTerminal({
    sessionId, cols: 100, rows: 30, onState: (state) => states.push(state),
    write: (bytes, processed) => writes.push({ bytes, processed }),
  });
  connections.push(connection);
  return { connection, socket: TerminalSocketMock.instances.slice(-1)[0]!, states, writes };
}

describe("Bidirectional terminal transport", () => {
  it("selects the exact session and dimensions using the terminal subprotocol", () => {
    vi.stubEnv("VITE_WS_URL", "wss://daemon.example/base");
    const { socket } = connect("session/one");
    expect(socket.url).toBe("wss://daemon.example/base/ws/terminal/session%2Fone?cols=100&rows=30");
    expect(socket.protocols).toEqual(["aq-terminal-v1"]);
    expect(socket.binaryType).toBe("arraybuffer");
    expect(socket.send).not.toHaveBeenCalled();
  });

  it("sends consecutive input immediately without HTTP or output acknowledgements", () => {
    const { connection, socket } = connect();
    connection.sendInput(encoder.encode("discard before ready"));
    socket.open();
    connection.sendInput(encoder.encode("discard before PTY ready"));
    expect(socket.send).not.toHaveBeenCalled();
    socket.ready();
    connection.sendInput(encoder.encode("h"));
    connection.sendInput(encoder.encode("i"));
    connection.sendInput(encoder.encode("\r"));
    expect(socket.inputs().map((data) => new TextDecoder().decode(data))).toEqual(["h", "i", "\r"]);
  });

  it("preserves raw colors, cursor controls and split UTF-8; ACKs only rendered bytes", () => {
    const { socket, writes } = connect();
    socket.ready();
    const first = Uint8Array.from([...encoder.encode("\x1b[38;2;255;100;0m\x1b[48;5;196m\x1b[7m"), 0xe4]);
    const second = Uint8Array.from([0xb8, 0x96, ...encoder.encode("\x1b[0m\x1b[2;3H")]);
    socket.message(first);
    socket.message(second);
    expect(writes.map(({ bytes }) => bytes)).toEqual([first, second]);
    expect(socket.controls()).toEqual([]);
    writes[0]!.processed();
    expect(socket.controls()).toEqual([{ type: "ack", bytes: first.length }]);
    writes[1]!.processed();
    expect(socket.controls()).toEqual([{ type: "ack", bytes: first.length }, { type: "ack", bytes: second.length }]);
  });

  it("bounds paste frames without splitting or losing bytes", () => {
    const { socket, connection } = connect();
    socket.ready();
    const bytes = encoder.encode("世界".repeat(12_000));
    connection.sendInput(bytes);
    expect(socket.inputs().every((frame) => frame.length <= 64 * 1024)).toBe(true);
    const received = Uint8Array.from(socket.inputs().flatMap((frame) => [...frame]));
    expect(received.length).toBe(bytes.length);
    expect(received.every((value, index) => value === bytes[index])).toBe(true);
  });

  it("disconnects a stalled input channel without queuing or replaying the next key", () => {
    const { socket, connection, states } = connect();
    socket.ready();
    socket.bufferedAmount = 256 * 1024;
    connection.sendInput(encoder.encode("do not queue"));
    expect(socket.inputs()).toEqual([]);
    expect(socket.closed).toBe(true);
    expect(states.slice(-1)[0]).toMatchObject({ status: "error", message: expect.stringMatching(/input.*backlog|keep up/i) });
    connection.sendInput(encoder.encode("\r"));
    expect(socket.inputs()).toEqual([]);
    expect(TerminalSocketMock.instances).toHaveLength(1);
  });

  it("coalesces resize while connecting and clamps dimensions for tiled views", () => {
    const { socket, connection } = connect();
    connection.resize(50, 12);
    connection.resize(60, 15);
    expect(socket.send).not.toHaveBeenCalled();
    socket.ready();
    expect(socket.controls()).toEqual([{ type: "resize", cols: 60, rows: 15 }]);
    connection.resize(60, 15);
    expect(socket.controls()).toHaveLength(1);
    connection.resize(900, 400);
    expect(socket.controls().slice(-1)[0]).toEqual({ type: "resize", cols: 500, rows: 300 });
    connection.resize(0, 0);
    expect(socket.controls().slice(-1)[0]).toEqual({ type: "resize", cols: 2, rows: 1 });
  });

  it("does not replay input or ACK stale output after disconnect/unmount", () => {
    const { socket, connection, writes, states } = connect();
    socket.ready();
    socket.message(encoder.encode("pending render"));
    socket.serverClose();
    connection.sendInput(encoder.encode("never send"));
    writes[0]!.processed();
    expect(socket.send).not.toHaveBeenCalled();
    expect(states.slice(-1)[0]?.status).toBe("disconnected");
    connection.close();
    expect(socket.onmessage).toBeNull();
    expect(TerminalSocketMock.instances).toHaveLength(1);
  });

  it("keeps each tiled terminal independent and detaches only the closed viewer", () => {
    const a = connect("session-a");
    const b = connect("session-b");
    a.socket.ready(); b.socket.ready();
    a.connection.sendInput(encoder.encode("a"));
    b.connection.sendInput(encoder.encode("b"));
    a.connection.close();
    expect(a.socket.inputs().map((bytes) => [...bytes])).toEqual([[97]]);
    expect(b.socket.inputs().map((bytes) => [...bytes])).toEqual([[98]]);
    expect(a.socket.closed).toBe(true);
    expect(b.socket.closed).toBe(false);
  });

  it.each(["error", "exit"])("preserves server %s state when the socket closes", (type) => {
    const { socket, states } = connect();
    socket.ready();
    socket.message(JSON.stringify({ type, message: "Session unavailable" }));
    socket.serverClose();
    expect(states.slice(-1)[0]?.status).toBe(type === "exit" ? "exited" : "error");
    expect(socket.closed).toBe(true);
  });

  it("does not re-enable input if the initial resize cannot be sent", () => {
    const { socket, connection, states } = connect();
    connection.resize(60, 15);
    socket.send.mockImplementation(() => { throw new Error("Socket closed during handshake"); });
    socket.ready();
    expect(states.slice(-1)[0]?.status).toBe("error");
    expect(socket.closed).toBe(true);
    connection.sendInput(encoder.encode("do not retry"));
    expect(socket.send).toHaveBeenCalledTimes(1);
  });

  it("bounds unprocessed output instead of silently dropping VT bytes", () => {
    const { socket, writes, states } = connect();
    socket.ready();
    for (let i = 0; i < 17; i++) socket.message(new Uint8Array(16 * 1024).fill(65));
    expect(writes).toHaveLength(16);
    expect(socket.closed).toBe(true);
    expect(states.slice(-1)[0]).toMatchObject({ status: "error", message: expect.stringMatching(/render buffer/i) });
    writes.forEach(({ processed }) => processed());
    expect(socket.controls()).toEqual([]);
  });

  it("refuses input if the server announces a different session", () => {
    const { socket, connection, states } = connect();
    socket.open();
    socket.message(JSON.stringify({ type: "ready", session_id: "another-session", cols: 100, rows: 30 }));
    connection.sendInput(encoder.encode("private input"));
    expect(socket.inputs()).toEqual([]);
    expect(states.slice(-1)[0]?.status).toBe("error");
  });
});
