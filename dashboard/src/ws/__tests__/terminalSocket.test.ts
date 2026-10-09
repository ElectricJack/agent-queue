import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { connectTerminal, type TerminalConnection, type TerminalConnectionState } from "../terminalSocket";
import { TerminalSocketMock } from "../../testUtils/terminal";

const api = vi.hoisted(() => ({ terminalAccess: vi.fn() }));
vi.mock("../../api/client", () => api);

const encoder = new TextEncoder();
let connections: TerminalConnection[] = [];

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(1);
  api.terminalAccess.mockReset().mockResolvedValue({ data: { status: "ready" } });
  TerminalSocketMock.instances = [];
  vi.stubEnv("VITE_TERMINAL_WS_URL", "");
  vi.stubGlobal("WebSocket", TerminalSocketMock);
});
afterEach(() => {
  connections.forEach((connection) => connection.close());
  connections = [];
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function connect(sessionId = "session-b") {
  const states: TerminalConnectionState[] = [];
  const writes: { bytes: Uint8Array; processed: () => void }[] = [];
  const connection = connectTerminal({
    sessionId, cols: 100, rows: 30, onState: (state) => states.push(state),
    write: (bytes, processed) => writes.push({ bytes, processed }),
  });
  vi.advanceTimersByTime(0);
  connections.push(connection);
  return { connection, socket: TerminalSocketMock.instances.slice(-1)[0]!, states, writes };
}

describe("Bidirectional terminal transport", () => {
  it("selects the exact session and dimensions using the terminal subprotocol", () => {
    vi.stubEnv("VITE_TERMINAL_WS_URL", "wss://daemon.example/base");
    const { socket } = connect("session/one");
    expect(socket.url).toBe("wss://daemon.example/base/ws/terminal/session%2Fone?cols=100&rows=30");
    expect(socket.protocols).toEqual(["aq-terminal-v1"]);
    expect(socket.binaryType).toBe("arraybuffer");
    expect(socket.send).not.toHaveBeenCalled();
  });

  it("uses the dashboard proxy despite legacy event and API URL overrides", () => {
    vi.stubEnv("VITE_WS_URL", "ws://127.0.0.1:8081");
    vi.stubEnv("VITE_API_URL", "http://127.0.0.1:8081");
    const { socket } = connect();
    const expected = new URL("/ws/terminal/session-b?cols=100&rows=30", window.location.href);
    expected.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    expect(socket.url).toBe(expected.toString());
  });

  it("uses secure same-origin WebSockets when the dashboard is served over HTTPS", () => {
    vi.stubEnv("VITE_WS_URL", "ws://127.0.0.1:8081");
    vi.stubGlobal("window", { location: { origin: "https://dashboard.example", href: "https://dashboard.example/agents" }, addEventListener: window.addEventListener.bind(window), removeEventListener: window.removeEventListener.bind(window) });
    const { socket } = connect();
    expect(socket.url).toBe("wss://dashboard.example/ws/terminal/session-b?cols=100&rows=30");
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
    expect(states.slice(-1)[0]).toMatchObject({ status: "reconnecting", message: expect.stringMatching(/input.*backlog|keep up/i) });
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
    expect(states.slice(-1)[0]?.status).toBe("reconnecting");
    connection.close();
    expect(socket.onmessage).toBeNull();
    expect(TerminalSocketMock.instances).toHaveLength(1);
  });

  it("never closes a CONNECTING socket and ignores its later events after close", () => {
    const { socket, connection, states, writes } = connect();
    connection.close();
    expect(socket.close).not.toHaveBeenCalled();
    expect(socket.onmessage).toBeNull();
    expect(socket.onerror).toBeNull();
    expect(socket.onclose).toBeNull();

    socket.message(JSON.stringify({ type: "ready", session_id: "session-b", cols: 100, rows: 30 }));
    expect(writes).toEqual([]);
    expect(states).toEqual([{ status: "connecting" }]);

    socket.open();
    expect(socket.close).toHaveBeenCalledOnce();
    expect(states).toEqual([{ status: "connecting" }]);
  });

  it("also defers a CONNECTING close when a connection error retries it", () => {
    const { socket, states } = connect();
    socket.onerror?.();
    vi.advanceTimersByTime(1_000);
    expect(socket.close).not.toHaveBeenCalled();
    expect(states.slice(-1)[0]).toMatchObject({ status: "reconnecting" });

    socket.open();
    expect(socket.close).toHaveBeenCalledOnce();
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
    expect(states.slice(-1)[0]?.status).toBe("reconnecting");
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
    expect(states.slice(-1)[0]).toMatchObject({ status: "reconnecting", message: expect.stringMatching(/render buffer/i) });
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

describe("Terminal recovery", () => {
  it("retries indefinitely with exponential backoff, jitter and a 15s cap", () => {
    const { socket, states } = connect();
    socket.serverClose();
    for (let attempt = 1; attempt <= 20; attempt++) {
      expect(states.slice(-1)[0]).toMatchObject({ status: "reconnecting", attempt });
      const delay = Math.min(15_000, 500 * 2 ** Math.min(attempt - 1, 5));
      const count = TerminalSocketMock.instances.length;
      vi.advanceTimersByTime(delay - 1);
      expect(TerminalSocketMock.instances).toHaveLength(count);
      vi.advanceTimersByTime(1);
      expect(TerminalSocketMock.instances).toHaveLength(count + 1);
      TerminalSocketMock.instances.slice(-1)[0]!.serverClose();
    }
    expect(vi.getTimerCount()).toBe(1);
  });

  it("jitters retry delays and only resets backoff after a stable connection", () => {
    vi.mocked(Math.random).mockReturnValue(0);
    const { socket, states } = connect();
    socket.ready(); socket.serverClose();
    vi.advanceTimersByTime(399);
    expect(TerminalSocketMock.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    let next = TerminalSocketMock.instances.slice(-1)[0]!;
    next.ready(); next.serverClose();
    expect(states.slice(-1)[0]?.attempt).toBe(2);
    vi.advanceTimersByTime(800);
    next = TerminalSocketMock.instances.slice(-1)[0]!;
    next.ready();
    vi.advanceTimersByTime(30_000);
    next.message(JSON.stringify({ type: "pong" }));
    next.serverClose();
    expect(states.slice(-1)[0]?.attempt).toBe(1);
  });

  it("pauses retries in a hidden/offline tab and wakes on visibility, online and daemon recovery", () => {
    const { socket } = connect();
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    socket.serverClose();
    vi.advanceTimersByTime(60_000);
    expect(TerminalSocketMock.instances).toHaveLength(1);
    vi.mocked(Object.getOwnPropertyDescriptor(document, "visibilityState")!.get!).mockReturnValue("visible");
    document.dispatchEvent(new Event("visibilitychange"));
    vi.advanceTimersByTime(0);
    expect(TerminalSocketMock.instances).toHaveLength(2);
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    TerminalSocketMock.instances.slice(-1)[0]!.serverClose();
    vi.advanceTimersByTime(60_000);
    expect(TerminalSocketMock.instances).toHaveLength(2);
    vi.mocked(Object.getOwnPropertyDescriptor(navigator, "onLine")!.get!).mockReturnValue(true);
    window.dispatchEvent(new Event("online"));
    vi.advanceTimersByTime(0);
    expect(TerminalSocketMock.instances).toHaveLength(3);
    TerminalSocketMock.instances.slice(-1)[0]!.serverClose();
    window.dispatchEvent(new Event("aq:connection-restored"));
    vi.advanceTimersByTime(0);
    expect(TerminalSocketMock.instances).toHaveLength(4);
  });

  it("discards input and stale ACKs across generations, then queues a screen reset", () => {
    const { socket, connection, writes } = connect();
    socket.ready();
    socket.message(encoder.encode("old pending output"));
    socket.serverClose();
    connection.sendInput(encoder.encode("discard"));
    connection.resize(60, 20);
    vi.advanceTimersByTime(500);
    const next = TerminalSocketMock.instances.slice(-1)[0]!;
    connection.sendInput(encoder.encode("discard until ready"));
    expect(next.url).toContain("cols=60&rows=20");
    next.ready();
    writes[0]!.processed();
    expect(next.controls()).toEqual([]);
    expect(writes[1]!.bytes).toEqual(Uint8Array.of(27, 99));
    next.message(encoder.encode("fresh pane"));
    writes[2]!.processed();
    expect(next.controls()).toEqual([{ type: "ack", bytes: 10 }]);
    expect(next.inputs()).toEqual([]);
  });

  it.each([4401, 4403, 1008, 4409, 4400])("stops on fatal close code %s", (code) => {
    const { socket, states, connection } = connect();
    socket.serverClose(code);
    connection.reconnect();
    window.dispatchEvent(new Event("online"));
    vi.advanceTimersByTime(60_000);
    expect(TerminalSocketMock.instances).toHaveLength(1);
    expect(states.slice(-1)[0]?.status).toBe(code === 4409 ? "exited" : "error");
  });

  it.each(["exit", "close"])("never retries after %s", (reason) => {
    const { socket, connection } = connect();
    socket.ready();
    if (reason === "exit") socket.message(JSON.stringify({ type: "exit" }));
    else { socket.serverClose(); connection.close(); }
    connection.reconnect();
    vi.advanceTimersByTime(60_000);
    expect(TerminalSocketMock.instances).toHaveLength(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([4401, 4403, 4409])("diagnoses opaque handshake refusal %s without retrying", async (code) => {
    api.terminalAccess.mockResolvedValue({ data: { status: code === 4409 ? "exited" : "error", code, message: "refused", retryable: false } });
    const { socket, states } = connect();
    socket.onerror?.(); socket.serverClose(1006);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(api.terminalAccess).toHaveBeenCalledOnce();
    expect(TerminalSocketMock.instances).toHaveLength(1);
    expect(states.slice(-1)[0]?.status).toBe(code === 4409 ? "exited" : "error");
    // Only a refused viewer is marked: an exited session is not a permission problem.
    expect(states.slice(-1)[0]?.refused).toBe(code === 4409 ? undefined : true);
  });

  it("stops on HTTP auth denial and retries an unreachable daemon", async () => {
    api.terminalAccess.mockRejectedValue(new Error("API 401: token required"));
    let viewer = connect();
    viewer.socket.serverClose(1006);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(viewer.states.slice(-1)[0]?.status).toBe("error");
    expect(TerminalSocketMock.instances).toHaveLength(1);
    api.terminalAccess.mockRejectedValue(new Error("API 503: daemon_unreachable"));
    viewer = connect();
    viewer.socket.serverClose(1006);
    await vi.advanceTimersByTimeAsync(500);
    expect(TerminalSocketMock.instances).toHaveLength(3);
    expect(viewer.states.slice(-1)[0]?.status).toBe("reconnecting");
  });

  it("ignores probe results after unmount or a newer connection", async () => {
    let resolve!: (value: unknown) => void;
    api.terminalAccess.mockImplementation(() => new Promise((done) => { resolve = done; }));
    const { socket, connection, states } = connect();
    socket.serverClose(1006);
    connection.reconnect();
    vi.advanceTimersByTime(0);
    const next = TerminalSocketMock.instances.slice(-1)[0]!;
    next.ready();
    resolve({ data: { status: "error", message: "old denial", retryable: false } });
    await Promise.resolve();
    expect(states.slice(-1)[0]?.status).toBe("connected");
    connection.close();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("retries stalled handshakes and silent keepalive failures, without sending terminal input", () => {
    const { socket, states } = connect();
    vi.advanceTimersByTime(15_000);
    expect(states.slice(-1)[0]).toMatchObject({ status: "reconnecting", message: expect.stringMatching(/handshake/i) });
    socket.open(); // detached orphan is closed, not used
    vi.advanceTimersByTime(500);
    const next = TerminalSocketMock.instances.slice(-1)[0]!;
    next.ready();
    vi.advanceTimersByTime(30_000);
    expect(next.controls()).toEqual([{ type: "ping" }, { type: "ping" }]);
    next.message(JSON.stringify({ type: "pong" }));
    vi.advanceTimersByTime(60_000);
    expect(states.slice(-1)[0]).toMatchObject({ status: "reconnecting", message: expect.stringMatching(/keepalive/i) });
    expect(next.inputs()).toEqual([]);
  });

  it("retries server backpressure/transport errors but stops on identity changes", () => {
    const { socket, states } = connect();
    socket.ready();
    socket.message(JSON.stringify({ type: "error", code: 4408, retryable: true, message: "ack timeout" }));
    expect(states.slice(-1)[0]?.status).toBe("reconnecting");
    vi.advanceTimersByTime(500);
    TerminalSocketMock.instances.slice(-1)[0]!.message(JSON.stringify({ type: "error", code: 4409, retryable: false, message: "instance changed" }));
    vi.advanceTimersByTime(60_000);
    expect(states.slice(-1)[0]?.status).toBe("exited");
    expect(TerminalSocketMock.instances).toHaveLength(2);
  });
});

it("does not reconnect an invisible terminal view until its layout returns", () => {
  const { connection, socket } = connect();
  connection.setVisible(false);
  socket.serverClose();
  connection.reconnect();
  vi.advanceTimersByTime(60_000);
  expect(TerminalSocketMock.instances).toHaveLength(1);
  connection.setVisible(true);
  vi.advanceTimersByTime(0);
  expect(TerminalSocketMock.instances).toHaveLength(2);
});

describe("Phone attach (mobile terminal)", () => {
  function connectPhone(options: { history?: number; restoreSize?: boolean } = {}) {
    const states: TerminalConnectionState[] = [];
    const connection = connectTerminal({
      sessionId: "session-b", cols: 44, rows: 20, ...options,
      onState: (state) => states.push(state), write: () => {},
    });
    vi.advanceTimersByTime(0);
    connections.push(connection);
    return { connection, socket: TerminalSocketMock.instances.slice(-1)[0]!, states };
  }

  it("asks for scrollback and a size restore only when the caller does, so a desktop attach is unchanged", () => {
    expect(connect().socket.url).toMatch(/\?cols=100&rows=30$/);
    const { socket } = connectPhone({ history: 2000, restoreSize: true });
    expect(new URL(socket.url).searchParams.get("history")).toBe("2000");
    expect(new URL(socket.url).searchParams.get("restore_size")).toBe("1");
    expect(new URL(connectPhone({ history: 50_000.7 }).socket.url).searchParams.get("history")).toBe("10000");
    expect(new URL(connectPhone({ history: 0 }).socket.url).searchParams.has("history")).toBe(false);
  });

  it("asks again on every reconnect, at the current size", async () => {
    const { connection, socket } = connectPhone({ history: 2000, restoreSize: true });
    socket.ready();
    connection.resize(50, 22);
    socket.serverClose(1006);
    await vi.advanceTimersByTimeAsync(1_000); // the access probe, then the backoff
    const next = new URL(TerminalSocketMock.instances.slice(-1)[0]!.url);
    expect(TerminalSocketMock.instances).toHaveLength(2);
    expect(Object.fromEntries(next.searchParams)).toEqual({ cols: "50", rows: "22", history: "2000", restore_size: "1" });
  });

  it.each([4401, 4403, 1008])("marks a close with %s as a refused viewer, without retrying", (code) => {
    const { socket, states } = connectPhone();
    socket.serverClose(code);
    vi.advanceTimersByTime(60_000);
    expect(states.slice(-1)[0]).toMatchObject({ status: "error", refused: true });
    expect(TerminalSocketMock.instances).toHaveLength(1);
  });

  it("marks a refusal error frame, but not a retryable transport error", () => {
    const { socket, states } = connectPhone();
    socket.ready();
    socket.message(JSON.stringify({ type: "error", code: 4408, retryable: true, message: "ack timeout" }));
    expect(states.slice(-1)[0]).toMatchObject({ status: "reconnecting" });
    expect(states.slice(-1)[0]?.refused).toBeUndefined();
    vi.advanceTimersByTime(500);
    TerminalSocketMock.instances.slice(-1)[0]!.message(JSON.stringify({ type: "error", code: 4403, retryable: false, message: "origin not allowed" }));
    expect(states.slice(-1)[0]).toMatchObject({ status: "error", refused: true, message: "origin not allowed" });
  });
});
