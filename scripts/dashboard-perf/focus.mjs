// Requests fired when a dashboard tab regains visibility after being hidden.
// Usage: node focus.mjs <baseUrl> [path] [awayMs]. See README.md.
import puppeteer from "puppeteer-core";
const [BASE, path = "/projects/agent-queue/tasks", awayMs = "20000"] = process.argv.slice(2);
const browser = await puppeteer.launch({ executablePath: process.env.CHROME ?? "/usr/bin/google-chrome", headless: "new", args: ["--no-sandbox"] });
const page = await browser.newPage();
await page.setViewport({ width: 1600, height: 1000 });
const cdp = await page.createCDPSession();
await cdp.send("Network.enable");
const api = []; const t = new Map();
cdp.on("Network.requestWillBeSent", (e) => { const u = new URL(e.request.url); if (u.pathname.startsWith("/api/")) { api.push({ at: Date.now(), path: u.pathname, id: e.requestId }); t.set(e.requestId, e.timestamp); } });
const done = new Map();
cdp.on("Network.loadingFinished", (e) => { if (t.has(e.requestId)) done.set(e.requestId, e.timestamp - t.get(e.requestId)); });
await page.goto(BASE + path, { waitUntil: "networkidle2" });
await new Promise((r) => setTimeout(r, 3000));
const setVis = (v) => page.evaluate((v) => { Object.defineProperty(document, "visibilityState", { configurable: true, get: () => v }); Object.defineProperty(document, "hidden", { configurable: true, get: () => v === "hidden" }); document.dispatchEvent(new Event("visibilitychange", { bubbles: true })); window.dispatchEvent(new Event(v === "hidden" ? "blur" : "focus")); }, v);
await setVis("hidden");
const hiddenAt = Date.now();
await new Promise((r) => setTimeout(r, Number(awayMs)));
const hid = api.filter((a) => a.at >= hiddenAt); const duringHidden = hid.length; const hby = {}; for (const b of hid) hby[b.path] = (hby[b.path] || 0) + 1; console.log("hidden", JSON.stringify(hby));
const t0 = Date.now();
await setVis("visible");
await new Promise((r) => setTimeout(r, 3000));
const burst = api.filter((a) => a.at >= t0 && a.at < t0 + 1500);
const by = {}; for (const b of burst) by[b.path] = (by[b.path] || 0) + 1;
const slowest = Math.max(0, ...burst.map((b) => (done.get(b.id) ?? 0) * 1000));
console.log(JSON.stringify({ away_ms: Number(awayMs), requests_while_hidden: duringHidden, focus_burst: burst.length, slowest_ms: Math.round(slowest), by_path: by }));
await browser.close();
