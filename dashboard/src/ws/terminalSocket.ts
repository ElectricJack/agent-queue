export type TerminalConnectionState = {
  status: "connecting" | "connected" | "disconnected" | "exited" | "error";
  message?: string;
};

export interface TerminalConnection {
  sendInput(bytes: Uint8Array): void;
  resize(cols: number, rows: number): void;
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

/** A viewer owns one socket; closing it detaches without stopping the agent. */
export function connectTerminal({ sessionId, cols, rows, write, onState }: {
  sessionId: string;
  cols: number;
  rows: number;
  write: (bytes: Uint8Array, processed: () => void) => void;
  onState: (state: TerminalConnectionState) => void;
}): TerminalConnection {
  let socket: WebSocket | undefined;
  let disposed = false;
  let ended = false;
  let ready = false;
  let outputPending = 0;
  let size = terminalDimensions(cols, rows);
  let sentSize = size;

  const finish = (state: TerminalConnectionState) => {
    if (disposed || ended) return;
    ended = true;
    ready = false;
    onState(state);
    socket?.close();
  };
  const fail = (message: string) => finish({ status: "error", message });
  const writable = () => !disposed && !ended && ready && socket?.readyState === WebSocket.OPEN;
  const sendControl = (control: object) => {
    if (!writable()) return;
    try { socket!.send(JSON.stringify(control)); }
    catch { fail("The terminal connection failed. Unsent input was discarded."); }
  };
  const sendSize = () => {
    if (!writable() || (size.cols === sentSize.cols && size.rows === sentSize.rows)) return;
    sendControl({ type: "resize", ...size });
    sentSize = size;
  };

  const connection: TerminalConnection = {
    sendInput(bytes) {
      // Never buffer input while connecting, disconnected, or reconnecting.
      if (!writable() || !bytes.byteLength) return;
      if (socket!.bufferedAmount + bytes.byteLength > MAX_INPUT_BACKLOG) {
        fail("The terminal input backlog cannot keep up. Unsent input was discarded.");
        return;
      }
      try {
        for (let offset = 0; offset < bytes.byteLength; offset += MAX_INPUT_FRAME) {
          socket!.send(bytes.subarray(offset, offset + MAX_INPUT_FRAME));
        }
      } catch { fail("The terminal connection failed. Unsent input was discarded."); }
    },
    resize(nextCols, nextRows) {
      if (!Number.isFinite(nextCols) || !Number.isFinite(nextRows)) return;
      size = terminalDimensions(nextCols, nextRows);
      sendSize();
    },
    close() {
      disposed = true;
      ready = false;
      if (!socket) return;
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
      socket.close();
    },
  };

  onState({ status: "connecting" });
  try {
    // The notification stream defaults to the page's own origin (VITE_WS_URL is
    // opt-in and unset by default).
    // Terminals must use the same-origin Vite proxy unless explicitly configured;
    // a separate endpoint also requires api_auth.trusted_dashboard_origins.
    const base = import.meta.env.VITE_TERMINAL_WS_URL || window.location.origin;
    const url = new URL(base, window.location.href);
    url.protocol = url.protocol === "https:" || url.protocol === "wss:" ? "wss:" : "ws:";
    url.pathname = url.pathname.replace(/\/$/, "") + "/ws/terminal/" + encodeURIComponent(sessionId);
    url.search = new URLSearchParams({ cols: String(size.cols), rows: String(size.rows) }).toString();
    url.hash = "";
    socket = new WebSocket(url.toString(), ["aq-terminal-v1"]);
    socket.binaryType = "arraybuffer";

    socket.onmessage = ({ data }: MessageEvent) => {
      if (disposed || ended) return;
      if (typeof data === "string") {
        try {
          const frame = JSON.parse(data);
          if (frame.type === "ready") {
            if (frame.session_id !== sessionId) {
              fail("The terminal server announced a different session.");
              return;
            }
            ready = true;
            sentSize = { cols: frame.cols, rows: frame.rows };
            sendSize();
            if (!ended) onState({ status: "connected" });
          } else if (frame.type === "error") {
            fail(typeof frame.message === "string" ? frame.message : "Terminal unavailable.");
          } else if (frame.type === "exit") {
            finish({ status: "exited", message: "The terminal session has ended." });
          } else {
            fail("The terminal server sent an unsupported control message.");
          }
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
        fail("Terminal output exceeded the render buffer. Reconnect to restore the screen.");
        return;
      }
      let acknowledged = false;
      try {
        // Keep VT sequences and split UTF-8 intact. Flow control follows the
        // renderer, never network arrival or a React update.
        write(bytes, () => {
          if (acknowledged) return;
          acknowledged = true;
          outputPending -= bytes.byteLength;
          sendControl({ type: "ack", bytes: bytes.byteLength });
        });
      } catch { fail("Terminal output could not be rendered."); }
    };
    socket.onclose = () => {
      if (disposed || ended) return;
      ended = true;
      ready = false;
      onState({ status: "disconnected", message: "Terminal disconnected. Unsent input was discarded." });
    };
    socket.onerror = () => fail("Could not connect to the terminal. Check the daemon connection.");
  } catch { fail("Could not open the terminal connection."); }
  return connection;
}
