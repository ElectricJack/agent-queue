import { terminalAccess } from "../api/client";
import { reconnectLoop } from "./reconnect";

export type TerminalConnectionState = {
  status: "connecting" | "connected" | "reconnecting" | "exited" | "error";
  message?: string;
  attempt?: number;
  /** The daemon refused this viewer an attach (credentials, origin, loopback). */
  refused?: true;
};

export interface TerminalConnection {
  sendInput(bytes: Uint8Array): void;
  resize(cols: number, rows: number): void;
  setVisible(visible: boolean): void;
  reconnect(): void;
  close(): void;
}

const MAX_INPUT_FRAME = 64 * 1024;
const MAX_INPUT_BACKLOG = 128 * 1024;
const MAX_OUTPUT_BACKLOG = 256 * 1024;

export function terminalDimensions(cols: number, rows: number) {
  return {
    cols: Math.min(500, Math.max(2, Math.floor(cols))),
    rows: Math.min(300, Math.max(1, Math.floor(rows))),
  };
}

const REFUSED = "Terminal access refused. Check credentials, origin and loopback access.";

/**
 * A viewer owns one connection; closing it detaches without stopping the agent.
 * A phone attach also asks for `history` lines of scrollback ahead of the live
 * screen and for `restoreSize`: the agent's window gets its size back when the
 * last viewer leaves, so a phone never leaves it phone-sized. A daemon without
 * either option ignores the query, and a desktop attach sends neither.
 */
export function connectTerminal({ sessionId, cols = 80, rows = 24, write, onState, visible = true, history = 0, restoreSize = false }: {
  sessionId: string;
  cols?: number;
  rows?: number;
  write: (bytes: Uint8Array, processed: () => void) => void;
  onState: (state: TerminalConnectionState) => void;
  visible?: boolean;
  history?: number;
  restoreSize?: boolean;
}): TerminalConnection {
  let socket: WebSocket | undefined;
  let ended = false;
  let ready = false;
  let hadReady = false;
  let size = terminalDimensions(cols, rows);
  let sentSize = size;
  let deadline: ReturnType<typeof setTimeout> | undefined;
  let keepalive: ReturnType<typeof setInterval> | undefined;
  let probe: AbortController | undefined;
  let retryMessage = "Terminal disconnected. Unsent input was discarded.";

  const base = import.meta.env.VITE_TERMINAL_WS_URL || window.location.origin;
  const url = new URL(base, window.location.href);
  url.protocol = url.protocol === "https:" || url.protocol === "wss:" ? "wss:" : "ws:";
  url.pathname = url.pathname.replace(/\/$/, "") + "/ws/terminal/" + encodeURIComponent(sessionId);
  url.hash = "";

  const retry = reconnectLoop(open, (attempt) => onState({ status: "reconnecting", attempt, message: retryMessage }));
  retry.setVisible(visible);
  const closeSocket = () => {
    ready = false;
    clearTimeout(deadline);
    clearInterval(keepalive);
    probe?.abort();
    probe = undefined;
    if (!socket) return;
    const current = socket;
    socket = undefined;
    current.onopen = current.onmessage = current.onerror = current.onclose = null;
    // Browsers report close() during CONNECTING as an error. StrictMode's
    // throwaway connection is cancelled before any socket is created.
    if (current.readyState === WebSocket.CONNECTING) {
      current.onopen = () => { current.onopen = null; current.close(); };
    } else if (current.readyState === WebSocket.OPEN) current.close();
  };
  const finish = (state: TerminalConnectionState) => {
    if (ended) return;
    ended = true;
    retry.stop();
    closeSocket();
    onState(state);
  };
  const fail = (message: string) => finish({ status: "error", message });
  const refuse = (message = REFUSED) => finish({ status: "error", message, refused: true });
  const dropped = (message = "Terminal disconnected. Unsent input was discarded.") => {
    if (ended) return;
    retryMessage = message;
    closeSocket();
    retry.retry();
  };
  const writable = () => !ended && ready && socket?.readyState === WebSocket.OPEN;
  const sendControl = (control: object) => {
    if (!writable()) return;
    try { socket!.send(JSON.stringify(control)); }
    catch { dropped("The terminal connection failed. Unsent input was discarded."); }
  };
  const sendSize = () => {
    if (!writable() || (size.cols === sentSize.cols && size.rows === sentSize.rows)) return;
    sendControl({ type: "resize", ...size });
    sentSize = size;
  };

  async function diagnose(current: WebSocket) {
    // HTTP can explain a refused handshake; WebSocket's 1006 cannot. Retry
    // transport failures, but stop on a confirmed auth/session refusal.
    const controller = new AbortController();
    probe = controller;
    const timeout = setTimeout(() => controller.abort(), 5_000);
    try {
      const probeUrl = new URL(base, window.location.href);
      probeUrl.protocol = url.protocol === "wss:" ? "https:" : "http:";
      const { data } = await terminalAccess({
        baseUrl: probeUrl.toString().replace(/\/$/, ""),
        path: { session_id: sessionId },
        query: { browser_origin: window.location.origin },
        signal: controller.signal,
      });
      if (ended || socket !== current || controller.signal.aborted) return;
      if (data && data.status !== "ready" && !data.retryable) {
        if (data.code === 4401 || data.code === 4403) refuse(data.message || REFUSED);
        else finish({ status: data.status === "exited" ? "exited" : "error", message: data.message });
        return;
      }
    } catch (error) {
      if (ended || socket !== current) return;
      // Middleware / the edge can deny the HTTP probe before the handler.
      if (error instanceof Error && /^API (401|403):/.test(error.message)) {
        refuse();
        return;
      }
    } finally {
      clearTimeout(timeout);
      if (probe === controller) probe = undefined;
    }
    if (!ended && socket === current) { closeSocket(); retry.resume(); }
  }

  function open() {
    if (ended) return;
    closeSocket();
    try {
      const query = new URLSearchParams({ cols: String(size.cols), rows: String(size.rows) });
      if (history > 0) query.set("history", String(Math.min(10_000, Math.floor(history))));
      if (restoreSize) query.set("restore_size", "1");
      url.search = query.toString();
      const current = new WebSocket(url.toString(), ["aq-terminal-v1"]);
      socket = current;
      current.binaryType = "arraybuffer";
      let outputPending = 0;
      let lastPong = Date.now();
      let awaitingPong = false;
      const live = () => !ended && socket === current;
      deadline = setTimeout(() => {
        if (live()) dropped("Terminal handshake timed out. Unsent input was discarded.");
      }, 15_000);
      current.onmessage = ({ data }: MessageEvent) => {
        if (!live()) return;
        if (typeof data === "string") {
          try {
            const frame = JSON.parse(data);
            if (frame.type === "ready") {
              if (ready || frame.session_id !== sessionId) {
                fail("The terminal server announced an invalid session.");
                return;
              }
              // Queue RIS after old writes and before the fresh tmux attach's
              // redraw. It clears stale parser/screen state, without input.
              if (hadReady) write(Uint8Array.of(27, 99), () => {});
              hadReady = true;
              ready = true;
              sentSize = { cols: frame.cols, rows: frame.rows };
              sendSize();
              if (!live() || !ready) return;
              clearTimeout(deadline);
              retry.connected();
              onState({ status: "connected" });
              keepalive = setInterval(() => {
                if (!live() || !ready) return;
                if (!retry.available()) { awaitingPong = false; lastPong = Date.now(); return; }
                if (awaitingPong && Date.now() - lastPong >= 45_000) {
                  dropped("Terminal keepalive timed out. Unsent input was discarded.");
                  return;
                }
                if (!awaitingPong) { awaitingPong = true; lastPong = Date.now(); }
                sendControl({ type: "ping" });
              }, 15_000);
            } else if (frame.type === "pong") {
              awaitingPong = false;
            } else if (frame.type === "error") {
              const message = typeof frame.message === "string" ? frame.message : "Terminal unavailable.";
              if (frame.retryable && ![4401, 4403, 4409].includes(frame.code)) dropped(message);
              else if ([4401, 4403].includes(frame.code)) refuse(message);
              else finish({ status: frame.code === 4409 ? "exited" : "error", message });
            } else if (frame.type === "exit") {
              finish({ status: "exited", message: "The terminal session has ended." });
            } else fail("The terminal server sent an unsupported control message.");
          } catch { fail("The terminal server sent an invalid control message."); }
          return;
        }
        if (!ready || !(data instanceof ArrayBuffer)) {
          fail("The terminal server sent invalid output.");
          return;
        }
        const bytes = new Uint8Array(data);
        if (!bytes.byteLength) return;
        outputPending += bytes.byteLength;
        if (outputPending > MAX_OUTPUT_BACKLOG) {
          dropped("Terminal output exceeded the render buffer. Restoring the screen.");
          return;
        }
        let acknowledged = false;
        try {
          write(bytes, () => {
            if (acknowledged || !live() || !ready) return;
            acknowledged = true;
            outputPending -= bytes.byteLength;
            sendControl({ type: "ack", bytes: bytes.byteLength });
          });
        } catch { fail("Terminal output could not be rendered."); }
      };
      current.onclose = (event: CloseEvent) => {
        if (!live()) return;
        ready = false;
        clearTimeout(deadline);
        clearInterval(keepalive);
        if ([4401, 4403, 1008].includes(event.code)) {
          refuse();
        } else if (event.code === 4409) {
          finish({ status: "exited", message: "The terminal session has ended or changed." });
        } else if (event.code === 4400) {
          fail("The terminal server refused the terminal protocol.");
        } else if (event.code === 1006) {
          retryMessage = "Terminal disconnected. Checking access; unsent input was discarded.";
          retry.retry(true);
          void diagnose(current);
        } else dropped();
      };
      // Browsers emit error before close. Wait for close to preserve auth codes;
      // the handshake deadline also covers a peer that never emits close.
      current.onerror = () => {
        if (live()) {
          ready = false;
          onState({ status: "reconnecting", attempt: retry.attempt + 1, message: "Terminal connection failed. Unsent input was discarded." });
          clearTimeout(deadline);
          deadline = setTimeout(() => { if (live()) dropped(); }, 1_000);
        }
      };
    } catch { dropped("Could not open the terminal connection."); }
  }

  onState({ status: "connecting" });
  retry.start();
  return {
    sendInput(bytes) {
      // Never buffer input while connecting, disconnected, or reconnecting.
      if (!writable() || !bytes.byteLength) return;
      if (socket!.bufferedAmount + bytes.byteLength > MAX_INPUT_BACKLOG) {
        dropped("The terminal input backlog cannot keep up. Unsent input was discarded.");
        return;
      }
      try {
        for (let offset = 0; offset < bytes.byteLength; offset += MAX_INPUT_FRAME) {
          socket!.send(bytes.subarray(offset, offset + MAX_INPUT_FRAME));
        }
      } catch { dropped("The terminal connection failed. Unsent input was discarded."); }
    },
    resize(nextCols, nextRows) {
      if (!Number.isFinite(nextCols) || !Number.isFinite(nextRows)) return;
      size = terminalDimensions(nextCols, nextRows);
      sendSize();
    },
    setVisible: retry.setVisible,
    reconnect() { if (!ended && !ready && !retry.recover()) { dropped(); retry.recover(); } },
    close() { ended = true; retry.stop(); closeSocket(); },
  };
}
