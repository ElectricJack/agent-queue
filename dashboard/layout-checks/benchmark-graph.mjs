// Read-only live graph benchmark. Run from the repository root:
// node dashboard/layout-checks/benchmark-graph.mjs [base URL] [project id]
// No DB access, clicks, or mutations. Chrome: $CHROME or /usr/bin/google-chrome.
import puppeteer from "puppeteer-core";

const base = process.argv[2] ?? "http://127.0.0.1:5173";
const project = process.argv[3] ?? "agent-queue";
const browser = await puppeteer.launch({
  executablePath: process.env.CHROME ?? "/usr/bin/google-chrome",
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 1000 });
  const cdp = await page.createCDPSession();
  await cdp.send("Performance.enable");
  const requests = new Map();
  const completed = [];
  let lastTile = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/")) {
      requests.set(request, performance.now());
    }
  });
  page.on("requestfinished", async (request) => {
    const start = requests.get(request);
    if (start === undefined) return;
    requests.delete(request);
    const response = request.response();
    const url = new URL(request.url());
    if (url.pathname.endsWith("/graph/tiles")) lastTile = performance.now();
    completed.push({
      path: url.pathname + url.search,
      method: request.method(),
      status: response?.status(),
      elapsedMs: performance.now() - start,
      serverTiming: response?.headers()["server-timing"] ?? null,
    });
  });
  page.on("requestfailed", (request) => requests.delete(request));
  await page.evaluateOnNewDocument(() => {
    window.graphFirstCardMs = null;
    const probe = () => {
      const cards = document.querySelectorAll("[data-task-id]");
      if ([...cards].some((card) => (() => {
        const rect = card.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.bottom > 0
          && rect.left < innerWidth && rect.top < innerHeight;
      })())) {
        window.graphFirstCardMs = performance.now();
      } else requestAnimationFrame(probe);
    };
    requestAnimationFrame(probe);
  });
  await page.goto(`${base}/projects/${encodeURIComponent(project)}/graph`, {
    waitUntil: "domcontentloaded", timeout: 60_000,
  });
  await page.waitForFunction(() => window.graphFirstCardMs !== null, { timeout: 60_000 });
  // "Fully loaded" here means the initial viewport's tile requests settled,
  // not all off-screen tasks (which the tiled graph intentionally never loads).
  const deadline = performance.now() + 30_000;
  let tilesSettled = false;
  while (performance.now() < deadline) {
    const tilesPending = [...requests.keys()].some((r) => new URL(r.url()).pathname.endsWith("/graph/tiles"));
    if (lastTile && !tilesPending && performance.now() - lastTile >= 500) {
      tilesSettled = true;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const browserData = await page.evaluate(() => ({
    firstCardMs: window.graphFirstCardMs,
    resources: performance.getEntriesByType("resource")
      .filter((r) => new URL(r.name).pathname.startsWith("/api/"))
      .map((r) => ({ path: new URL(r.name).pathname + new URL(r.name).search,
        startMs: r.startTime, elapsedMs: r.duration, decodedBytes: r.decodedBodySize })),
  }));
  const metrics = (await cdp.send("Performance.getMetrics")).metrics;
  const tileResources = browserData.resources.filter((r) => r.path.endsWith("/graph/tiles") && r.decodedBytes > 0);
  console.log(JSON.stringify({
    url: `${base}/projects/${project}/graph`,
    measuredAt: new Date().toISOString(),
    ...browserData,
    tilesSettled,
    viewportTilesCompleteMs: tilesSettled && tileResources.length
      ? Math.max(...tileResources.map((r) => r.startMs + r.elapsedMs)) : null,
    requests: completed,
    cpu: Object.fromEntries(metrics.filter((m) => ["ScriptDuration", "LayoutDuration", "RecalcStyleDuration", "TaskDuration"].includes(m.name))
      .map((m) => [m.name + "Ms", m.value * 1000])),
  }, null, 2));
} finally {
  await browser.close();
}
