// Layout-check stub: the built bundle plus a fake daemon on 127.0.0.1:<ephemeral>.
// Static rules mirror src/dashboard_server/bundle.py _resolve: a listed file,
// else index.html unless the suffix is a static-asset suffix, else 404. Never
// bind a fixed port — 5173 is the operator's dashboard on this box and
// 8081/8082 are daemon ports.
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { readdir, readFile, stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json",
  ".woff2": "font/woff2", ".png": "image/png", ".ico": "image/x-icon",
};
const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

/** Every fixtures/*.mjs `routes` export, merged; a key defined twice is an error. */
export async function loadFixtures(dir) {
  const routes = new Map();
  for (const name of (await readdir(dir)).filter((n) => n.endsWith(".mjs")).sort()) {
    const module = await import(pathToFileURL(join(dir, name)).href);
    for (const [key, handler] of Object.entries(module.routes ?? {})) {
      if (routes.has(key)) throw new Error(`fixture route "${key}" is defined twice (again in ${name})`);
      routes.set(key, handler);
    }
  }
  const paneFrames = JSON.parse(await readFile(join(dir, "pane-frames.json"), "utf8"));
  return { routes, paneFrames };
}

function send(res, status, body) {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

async function isFile(path) {
  try { return (await stat(path)).isFile(); } catch { return false; }
}

// Mirrors STATIC_SUFFIXES in src/dashboard_server/bundle.py.
const STATIC_SUFFIXES = new Set([".html", ".htm", ".js", ".mjs", ".cjs", ".css", ".map", ".json", ".txt", ".xml",
  ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".bmp",
  ".woff", ".woff2", ".ttf", ".otf", ".eot", ".wasm", ".webmanifest", ".pdf", ".zip"]);

export function staticTarget(root, pathname, exists) {
  const relative = decodeURIComponent(pathname).replace(/^\/+/, "");
  const file = resolve(root, relative);
  if (file !== root && !file.startsWith(root + sep)) return null;
  if (relative && exists) return file;
  return STATIC_SUFFIXES.has(extname(relative).toLowerCase()) ? null : join(root, "index.html");
}

/** A server frame (never masked): FIN + opcode, then the payload. */
function wsFrame(opcode, payload) {
  const len = payload.length;
  let head;
  if (len < 126) head = Buffer.from([0x80 | opcode, len]);
  else if (len < 65536) head = Buffer.from([0x80 | opcode, 126, len >> 8, len & 255]);
  else {
    head = Buffer.alloc(10);
    head[0] = 0x80 | opcode;
    head[1] = 127;
    head.writeBigUInt64BE(BigInt(len), 2);
  }
  return Buffer.concat([head, payload]);
}

/** Client frames (masked) → onFrame(opcode, payload). Unfragmented, as browsers send small messages. */
function wsReader(onFrame) {
  let buffer = Buffer.alloc(0);
  return (chunk) => {
    buffer = Buffer.concat([buffer, chunk]);
    for (;;) {
      if (buffer.length < 2) return;
      const opcode = buffer[0] & 0x0f;
      const masked = buffer[1] & 0x80;
      let len = buffer[1] & 0x7f;
      let at = 2;
      if (len === 126) {
        if (buffer.length < 4) return;
        len = buffer.readUInt16BE(2);
        at = 4;
      } else if (len === 127) {
        if (buffer.length < 10) return;
        len = Number(buffer.readBigUInt64BE(2));
        at = 10;
      }
      const maskAt = at;
      if (masked) at += 4;
      if (buffer.length < at + len) return;
      const payload = Buffer.from(buffer.subarray(at, at + len));
      if (masked) for (let i = 0; i < len; i++) payload[i] ^= buffer[maskAt + (i % 4)];
      buffer = buffer.subarray(at + len);
      onFrame(opcode, payload);
    }
  };
}

function acceptUpgrade(req, socket, protocol) {
  const accept = createHash("sha1").update(req.headers["sec-websocket-key"] + WS_GUID).digest("base64");
  const offered = (req.headers["sec-websocket-protocol"] ?? "").split(",").map((p) => p.trim());
  const chosen = protocol && offered.includes(protocol) ? `Sec-WebSocket-Protocol: ${protocol}\r\n` : "";
  socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n${chosen}\r\n`);
}

export async function startStubServer({ distDir, fixtures }) {
  const root = resolve(distDir);
  const requests = [];
  const unhandled = [];
  const terminalUpgrades = [];
  const overrides = new Map();
  const paneStreams = new Map(); // sessionId -> Set<res>
  const paneFailures = new Map(); // sessionId -> status
  const eventSockets = new Set();
  const terminalSockets = new Set();
  const terminalScreens = new Map();
  const terminalViewers = [];

  function openPane(sessionId, res) {
    const failure = paneFailures.get(sessionId);
    if (failure) return send(res, failure, { detail: "pane stream refused by the check" });
    res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
    for (const frame of fixtures.paneFrames[sessionId] ?? fixtures.paneFrames["*"] ?? []) {
      res.write(`data: ${JSON.stringify(frame)}\n\n`);
    }
    const open = paneStreams.get(sessionId) ?? new Set();
    open.add(res);
    paneStreams.set(sessionId, open);
    res.on("close", () => open.delete(res));
  }

  const server = createServer(async (req, res) => {
    const url = new URL(req.url, "http://stub.invalid");
    const body = await readBody(req);
    requests.push({ method: req.method, path: url.pathname, body });
    const pane = url.pathname.match(/^\/api\/sessions\/([^/]+)\/pane$/);
    if (pane) return openPane(decodeURIComponent(pane[1]), res);
    const access = url.pathname.match(/^\/ws\/terminal\/([^/]+)$/);
    if (access && req.method === "GET") {
      // The access probe a browser makes after a refused upgrade (WebSocket's
      // 1006 says nothing): answered like the daemon's TerminalAccessResponse.
      return send(res, 200, terminalScreens.has(decodeURIComponent(access[1]))
        ? { status: "ready", code: 0, message: "", retryable: false }
        : { status: "error", code: 4403, message: "Terminal access refused by the layout check.", retryable: false });
    }
    if (url.pathname.startsWith("/api/") || url.pathname === "/health" || url.pathname === "/ready") {
      const key = `${req.method} ${url.pathname}`;
      const handler = overrides.get(key) ?? fixtures.routes.get(key);
      if (!handler) {
        unhandled.push(key);
        return send(res, 404, { detail: `no layout-check fixture for ${key}` });
      }
      const result = await handler(body ? JSON.parse(body) : {}, url);
      if (result && typeof result === "object" && "__status" in result) return send(res, result.__status, result.body ?? {});
      return send(res, 200, result);
    }
    const relative = decodeURIComponent(url.pathname).replace(/^\/+/, "");
    const target = staticTarget(root, url.pathname, relative ? await isFile(resolve(root, relative)) : false);
    if (!target) return send(res, 404, { detail: "not found" });
    res.writeHead(200, { "content-type": TYPES[extname(target)] ?? "application/octet-stream" });
    createReadStream(target).pipe(res);
  });

  server.on("upgrade", (req, socket) => {
    const path = new URL(req.url, "http://stub.invalid").pathname;
    socket.on("error", () => {});
    if (path.startsWith("/ws/terminal")) {
      // An attach: refused unless the check allowed the session a screen.
      terminalUpgrades.push(req.url);
      const sessionId = decodeURIComponent(path.split("/")[3]);
      if (terminalScreens.has(sessionId)) {
        const record = { url: req.url, sessionId, frames: [], socket, open: true };
        terminalViewers.push(record);
        acceptUpgrade(req, socket, "aq-terminal-v1");
        socket.write(wsFrame(0x1, Buffer.from(JSON.stringify({ type: "ready", session_id: sessionId }))));
        socket.write(wsFrame(0x2, Buffer.from(terminalScreens.get(sessionId))));
        socket.on("data", wsReader((opcode, payload) => {
          if (opcode === 0x2) record.frames.push(payload.toString("utf8"));
          if (opcode === 0x1) {
            const control = JSON.parse(payload.toString("utf8"));
            record.frames.push(control);
            if (control.type === "ping") socket.write(wsFrame(0x1, Buffer.from(JSON.stringify({ type: "pong" }))));
          }
          if (opcode === 0x8) socket.end(wsFrame(0x8, payload.subarray(0, 2)));
        }));
        terminalSockets.add(socket);
        socket.on("close", () => { record.open = false; terminalSockets.delete(socket); });
        return;
      }
      socket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
      return;
    }
    if (path !== "/ws/events") return socket.destroy();
    acceptUpgrade(req, socket);
    socket.resume(); // Client frames (subscriptions, pings) are ignored.
    eventSockets.add(socket);
    socket.on("close", () => eventSockets.delete(socket));
  });

  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    requests, unhandled, terminalUpgrades,
    terminalViewers,
    // Attach is refused by default. Individual geometry checks opt in to a fake live PTY.
    allowTerminal: (sessionId, screen) => terminalScreens.set(sessionId, screen),
    /** Every binary input frame sent to `sessionId`, decoded, in order. */
    typed: (sessionId) => terminalViewers.filter((r) => r.sessionId === sessionId)
      .flatMap((r) => r.frames.filter((f) => typeof f === "string")),
    /** Live output to every open attach of `sessionId`. */
    terminalWrite(sessionId, text) {
      for (const viewer of terminalViewers) {
        if (viewer.sessionId === sessionId && viewer.open) viewer.socket.write(wsFrame(0x2, Buffer.from(text)));
      }
    },
    statePuts: () => requests.filter((r) => r.path === "/api/dashboard/state-put").map((r) => JSON.parse(r.body)),
    override: (key, handler) => overrides.set(key, handler),
    pushEvent(frame) {
      const encoded = wsFrame(1, Buffer.from(JSON.stringify(frame)));
      for (const socket of eventSockets) socket.write(encoded);
    },
    pushPane(sessionId, frame) {
      for (const res of paneStreams.get(sessionId) ?? []) res.write(`data: ${JSON.stringify(frame)}\n\n`);
    },
    dropPane(sessionId) {
      for (const res of paneStreams.get(sessionId) ?? []) res.destroy();
    },
    failPane(sessionId, status) {
      if (status) paneFailures.set(sessionId, status);
      else paneFailures.delete(sessionId);
    },
    dropEvents() {
      for (const socket of eventSockets) socket.destroy();
    },
    async close() {
      for (const socket of eventSockets) socket.destroy();
      for (const socket of terminalSockets) socket.destroy();
      for (const open of paneStreams.values()) for (const res of open) res.destroy();
      server.closeAllConnections();
      await new Promise((done) => server.close(done));
    },
  };
}
