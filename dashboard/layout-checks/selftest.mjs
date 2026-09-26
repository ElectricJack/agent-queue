// Probe self-test: a planted overflow, a clipped box, a 30 px "primary" button
// and a terminal WebSocket must each be caught, while an allowed scroller and an
// ellipsis are not. Needs Chrome, not the bundle.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { expectLayout, overflow, watchSockets } from "./probes.mjs";
import { PROFILES } from "./profiles.mjs";

const html = `<!doctype html><meta name=viewport content="width=device-width">
<body style="margin:0"><div id=wide style="width:600px;height:10px"></div>
<div id=clip style="overflow:hidden;width:100px"><div style="width:300px;height:10px"></div></div>
<div data-allow-overflow-x style="overflow-x:auto"><pre style="width:900px">terminal</pre></div>
<p style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100px">a truncated title that is much longer than its box</p>
<button data-primary-control style="height:30px;width:30px">x</button>
<script>new WebSocket("ws://" + location.host + "/ws/terminal/s")</script></body>`;
const server = createServer((req, res) => { res.writeHead(200, { "content-type": "text/html" }); res.end(html); });
server.on("upgrade", (req, socket) => socket.end("HTTP/1.1 403 Forbidden\r\n\r\n"));
await new Promise((done) => server.listen(0, "127.0.0.1", done));
const { default: puppeteer } = await import("puppeteer-core");
const browser = await puppeteer.launch({ executablePath: process.env.CHROME ?? "/usr/bin/google-chrome", headless: true, args: ["--no-sandbox"] });
try {
  const page = await browser.newPage();
  await page.setViewport(PROFILES["phone-390"].viewport);
  const sockets = await watchSockets(page);
  await page.goto(`http://127.0.0.1:${server.address().port}/`, { waitUntil: "load" });
  const found = await overflow(page);
  assert.ok(found.scroll > 1, "planted overflow not detected");
  assert.deepEqual(found.offenders.map((o) => o.kind).sort(), ["content wider than its box", "escapes the viewport"],
    "#wide escapes and #clip hides content; the terminal scroller and the ellipsis are allowed");
  const t = { page, isPhone: true, profile: "phone-390" };
  await page.evaluate(() => { document.getElementById("wide").remove(); document.getElementById("clip").remove(); });
  await assert.rejects(expectLayout(t, { primary: ["[data-primary-control]"] }), /≥44 px/);
  await assert.rejects(expectLayout(t, { primary: ["[data-missing]"] }), /no visible primary control/);
  await new Promise((done) => setTimeout(done, 300));
  assert.equal(sockets.terminal().length, 1, "terminal WebSocket not observed");
  console.log("layout probe self-test passed");
} finally {
  await browser.close();
  server.close();
}
