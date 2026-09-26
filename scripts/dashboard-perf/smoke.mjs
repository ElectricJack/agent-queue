// Real Chrome regression smoke against an ephemeral local fixture; no daemon/database.
// Needs puppeteer-core (the root `npm install`) and CHROME (as in README.md).
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const dir = await mkdtemp(join(tmpdir(), "aq-perf-smoke-"));
const server = createServer((req, res) => {
  req.resume();
  const isPage = req.url.startsWith("/projects/") || req.url === "/agents";
  const body = isPage
    ? '<main><a href="/projects/fixture/tasks" onclick="event.preventDefault()">Tasks</a><table><tr data-task-row><td>Fixture</td></tr></table><button aria-label="Open pool fixture">Pool</button></main>'
    : JSON.stringify(req.url === "/api/task/list" ? { tasks: [{ id: "fixture-1" }] } : {});
  res.writeHead(200, { "content-type": isPage ? "text/html" : "application/json" });
  res.end(body);
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const base = `http://127.0.0.1:${server.address().port}`;
let child;
try {
  const out = join(dir, "smoke.json");
  child = spawn(process.execPath, [fileURLToPath(new URL("harness.mjs", import.meta.url)),
    base, out, "--api", base, "--clients", "3", "--warmup-ms", "50",
    "--observe-ms", "200", "--idle-ms", "999", "--runs", "1", "--only", "tasks,agents",
    "--project", "fixture", "--no-interactions"], { detached: true, stdio: "inherit" });
  const code = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      process.kill(-child.pid, "SIGTERM");
      reject(new Error("harness smoke exceeded 60 seconds"));
    }, 60000);
    child.on("error", (error) => { clearTimeout(timer); reject(error); });
    child.on("exit", (value) => { clearTimeout(timer); resolve(value); });
  });
  assert.equal(code, 0);
  const data = JSON.parse(await readFile(out, "utf8"));
  assert.equal(data.manifest.clients, 3);
  assert.equal(data.manifest.observe_ms, 200); // Alias precedence.
  assert.equal(data.manifest.warmup_ms, 50);
  assert.match(data.manifest.chrome, /^Chrome\//);
  // Every surface gets its own set of concurrent clients, each in a separate window.
  for (const surface of ["tasks", "agents"]) {
    const clients = data.idle[surface].clients;
    assert.equal(clients.length, 3);
    assert.ok(Math.max(...clients.map((c) => c.start_ts)) < Math.min(...clients.map((c) => c.end_ts)));
    assert.equal(new Set(clients.map((c) => c.window_id)).size, 3);
  }
  assert.equal(data.cold.tasks.raw_samples.length, 1);
  assert.equal(data.warm.tasks.raw_samples.length, 1);
  assert.equal(data.api["POST /api/task/get"].raw_ms.length, 30);
  assert.equal(data.api["POST /api/task/get"].errors, 0);
  console.log("Chrome smoke passed: three concurrent windowed clients on two surfaces, duration alias, manifests, raw browser/API samples");
} finally {
  if (child?.pid) {
    try { process.kill(-child.pid, "SIGKILL"); } catch (error) {
      if (error.code !== "ESRCH") throw error;
    }
  }
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
  await rm(dir, { recursive: true, force: true });
}
