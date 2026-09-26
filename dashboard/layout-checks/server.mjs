// Layout-check stub: the built bundle plus a fake daemon on 127.0.0.1:<ephemeral>.
// Static rules mirror src/dashboard_server/bundle.py _resolve: a listed file,
// else index.html for a suffix-less path, else 404. Never bind a fixed port —
// 5173 is the operator's dashboard on this box and 8081/8082 are daemon ports.
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

export function staticTarget(root, pathname, exists) {
  const relative = decodeURIComponent(pathname).replace(/^\/+/, "");
  const file = resolve(root, relative);
  if (file !== root && !file.startsWith(root + sep)) return null;
  if (relative && exists) return file;
  return extname(relative) ? null : join(root, "index.html");
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
      // A focus route or a compact terminal must never get here (spec §4).
      terminalUpgrades.push(req.url);
      socket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
      return;
    }
    if (path !== "/ws/events") return socket.destroy();
    const accept = createHash("sha1").update(req.headers["sec-websocket-key"] + WS_GUID).digest("base64");
    socket.write(`HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`);
    socket.resume(); // Client frames (subscriptions, pings) are ignored: no events are ever sent.
    eventSockets.add(socket);
    socket.on("close", () => eventSockets.delete(socket));
  });

  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  return {
    url: `http://127.0.0.1:${server.address().port}`,
    requests, unhandled, terminalUpgrades,
    statePuts: () => requests.filter((r) => r.path === "/api/dashboard/state-put").map((r) => JSON.parse(r.body)),
    override: (key, handler) => overrides.set(key, handler),
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
      for (const open of paneStreams.values()) for (const res of open) res.destroy();
      server.closeAllConnections();
      await new Promise((done) => server.close(done));
    },
  };
}
