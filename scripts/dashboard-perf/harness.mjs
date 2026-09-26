// Dashboard performance harness: puppeteer-core over CDP against a served build.
// Usage: node harness.mjs <baseUrl> <outJson> [--api URL] [--clients N] [--warmup-ms N] [--observe-ms N] [--idle-ms N] [--runs N] [--cpu N] [--only a,b] [--project id] [--no-interactions] [--task-detail-only]
// See README.md next to this file.
import { probeApi } from "./api.mjs";
import { writeFileSync } from "node:fs";

const args = process.argv.slice(2);
const BASE = args[0];
const OUT = args[1];
const opt = (name, dflt) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : dflt;
};
const API = opt("--api", "");
const CLIENTS = Number(opt("--clients", "1"));
const WARMUP_MS = Number(opt("--warmup-ms", "0"));
const IDLE_MS = Number(opt("--observe-ms", opt("--idle-ms", "60000")));
const RUNS = Number(opt("--runs", "3"));
const CPU = Number(opt("--cpu", "1"));
const ONLY = opt("--only", "")?.split(",").filter(Boolean);
const PROJECT = opt("--project", "agent-queue");

// Installed before any page script: long tasks, event timing, React commits.
const INSTRUMENT = () => {
  const p = (window.__perf = {
    longtasks: [],
    events: [],
    commits: 0,
    fibers: 0,
    names: {},
    trackNames: false,
  });
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) p.longtasks.push({ start: e.startTime, dur: e.duration });
    }).observe({ type: "longtask", buffered: true });
  } catch {}
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) {
        if (!e.interactionId) continue;
        p.events.push({
          name: e.name,
          start: e.startTime,
          dur: e.duration,
          proc: e.processingEnd - e.processingStart,
          delay: e.processingStart - e.startTime,
          id: e.interactionId,
        });
      }
    }).observe({ type: "event", buffered: true, durationThreshold: 16 });
  } catch {}
  const COMPONENT_TAGS = new Set([0, 1, 11, 14, 15]);
  function walk(fiber) {
    // Count fibers whose render function ran in this commit; skip subtrees a
    // bailout left untouched (child pointer shared with the alternate).
    const stack = [fiber];
    while (stack.length) {
      const f = stack.pop();
      if (COMPONENT_TAGS.has(f.tag) && (f.flags & 1) === 1) {
        p.fibers++;
        if (p.trackNames) {
          const t = f.type && (f.type.type || f.type.render || f.type);
          const n = (t && (t.displayName || t.name)) || "?";
          p.names[n] = (p.names[n] || 0) + 1;
        }
      }
      let c = f.child;
      if (c && f.alternate && f.alternate.child === c) {
        // Unchanged subtree — but siblings of f are still visited below.
        c = null;
      }
      while (c) {
        stack.push(c);
        c = c.sibling;
      }
    }
  }
  window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {
    supportsFiber: true,
    renderers: new Map(),
    inject() {
      return 1;
    },
    onScheduleFiberRoot() {},
    onCommitFiberRoot(_id, root) {
      p.commits++;
      try {
        walk(root.current);
      } catch {}
    },
    onCommitFiberUnmount() {},
    onPostCommitFiberRoot() {},
    checkDCE() {},
  };
  // Resolve with the rAF timestamp at which pred() first holds.
  window.__waitFor = (src, timeout = 15000) =>
    new Promise((resolve) => {
      const pred = new Function("return (" + src + ")()");
      const t0 = performance.now();
      let finished = false;
      const finish = (value) => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        resolve(value);
      };
      // rAF may stop in a background/frozen page; readiness must still time out.
      const timer = setTimeout(() => finish(-1), timeout);
      const tick = () => {
        if (finished) return;
        let ok = false;
        try {
          ok = !!pred();
        } catch {}
        if (ok) {
          // Next frame after the DOM holds: the paint that shows it.
          requestAnimationFrame(() => finish(performance.now()));
          return;
        }
        if (performance.now() - t0 > timeout) return finish(-1);
        requestAnimationFrame(tick);
      };
      tick();
    });
};

const noLoading = `(() => { const m = document.querySelector('main'); return !!m && !/Loading…/.test(m.innerText); })`;
const SURFACES = {
  graph: {
    path: `/projects/${PROJECT}/graph`,
    ready: `() => document.querySelectorAll('.react-flow__node').length > 0 && ${noLoading}()`,
  },
  tasks: {
    path: `/projects/${PROJECT}/tasks`,
    ready: `() => document.querySelectorAll('[data-task-row]').length > 0`,
  },
  reviews: {
    path: `/reviews`,
    ready: `() => !!document.querySelector('select[aria-label="Review state"]') && ${noLoading}()`,
  },
  metrics: {
    path: `/metrics`,
    ready: `() => document.querySelectorAll('main canvas').length > 0 && ${noLoading}()`,
  },
  agents: {
    path: `/agents`,
    ready: `() => document.querySelectorAll('[aria-label^="Open pool "]').length > 0`,
  },
  sessions: {
    path: `/projects/${PROJECT}/sessions`,
    ready: `() => { const m = document.querySelector('main'); return !!m && ${noLoading}() && m.querySelectorAll('tbody tr').length > 0; }`,
  },
  overview: {
    path: `/projects/${PROJECT}/overview`,
    ready: `() => { const m = document.querySelector('main'); return !!m && ${noLoading}() && m.innerText.length > 200; }`,
  },
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const median = (xs) => {
  const v = xs.filter((x) => x != null && x >= 0).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
};

let clientWindow = 0;
async function newPage(browser, separateWindow = false) {
  let page;
  if (separateWindow) {
    // An inactive tab suspends requestAnimationFrame, including __waitFor.
    // Each measured client needs its own foreground window.
    const marker = `about:blank#aq-perf-client-${++clientWindow}`;
    const session = await browser.target().createCDPSession();
    try {
      await session.send("Target.createTarget", { url: marker, newWindow: true, background: false });
      page = await (await browser.waitForTarget((target) => target.url() === marker)).page();
    } finally {
      await session.detach();
    }
  } else {
    page = await browser.newPage();
  }
  await page.setViewport({ width: 1600, height: 1000 });
  const cdp = await page.createCDPSession();
  await cdp.send("Network.enable");
  if (CPU > 1) await cdp.send("Emulation.setCPUThrottlingRate", { rate: CPU });
  const net = { api: [], ws: 0, wsBytes: 0, bytes: 0, js: 0 };
  const reqs = new Map();
  cdp.on("Network.requestWillBeSent", (e) => {
    reqs.set(e.requestId, e.request.url);
    const u = new URL(e.request.url);
    if (u.pathname.startsWith("/api/") || u.pathname === "/health" || u.pathname === "/ready")
      net.api.push({ t: Date.now(), path: u.pathname, method: e.request.method });
  });
  cdp.on("Network.loadingFinished", (e) => {
    const url = reqs.get(e.requestId) || "";
    net.bytes += e.encodedDataLength;
    if (/\.js(\?|$)/.test(url)) net.js += e.encodedDataLength;
  });
  cdp.on("Network.webSocketFrameReceived", (e) => {
    net.ws++;
    net.wsBytes += e.response.payloadData.length;
  });
  await page.evaluateOnNewDocument(INSTRUMENT);
  // Never write the operator's roaming dashboard state: answer preference
  // writes locally, the way the daemon would, after a write-like delay.
  await page.setRequestInterception(true);
  page.on("request", (req) => {
    const u = new URL(req.url());
    if (req.method() === "POST" && (u.pathname === "/api/dashboard/state-put" || u.pathname === "/api/dashboard/state-reset")) {
      const body = JSON.parse(req.postData() || "{}");
      const document = {
        scope: "user", owner_id: "human:local-operator", namespace: body.namespace,
        subject: body.subject ?? null, revision: (body.base_revision ?? 0) + 1, exists: true,
        updated_at: Date.now() / 1000, value: body.value ?? {},
      };
      net.stubbedWrites = (net.stubbedWrites ?? 0) + 1;
      setTimeout(() => req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ success: true, document }) }), 30);
      return;
    }
    req.continue();
  });
  return { page, cdp, net };
}

async function snapshot(page) {
  return page.evaluate(() => ({
    lt: window.__perf.longtasks.slice(),
    ev: window.__perf.events.slice(),
    commits: window.__perf.commits,
    fibers: window.__perf.fibers,
    now: performance.now(),
  }));
}
function ltStats(lt, from, to) {
  const xs = lt.filter((l) => l.start >= from && l.start < to);
  return {
    count: xs.length,
    tbt: Math.round(xs.reduce((a, l) => a + Math.max(0, l.dur - 50), 0)),
    max: Math.round(Math.max(0, ...xs.map((l) => l.dur))),
  };
}
function interaction(ev, from) {
  const xs = ev.filter((e) => e.start >= from);
  if (!xs.length) return null;
  const max = xs.reduce((a, e) => (e.dur > a.dur ? e : a));
  return { dur: max.dur, name: max.name, proc: Math.round(max.proc), delay: Math.round(max.delay) };
}

/** Cold load: fresh page, navigate, time to the surface's ready predicate. */
async function coldLoad(browser, key) {
  const s = SURFACES[key];
  const { page, cdp, net } = await newPage(browser);
  await cdp.send("Performance.enable");
  await page.goto(BASE + s.path, { waitUntil: "domcontentloaded" });
  const readyAt = await page.evaluate((src) => window.__waitFor(src, 20000), s.ready);
  const metrics = Object.fromEntries((await cdp.send("Performance.getMetrics")).metrics.map((m) => [m.name, m.value]));
  await sleep(2500);
  const snap = await snapshot(page);
  const r = {
    script_ms: Math.round((metrics.ScriptDuration ?? 0) * 1000),
    task_ms: Math.round((metrics.TaskDuration ?? 0) * 1000),
    ready_ms: readyAt < 0 ? null : Math.round(readyAt),
    lt_load: ltStats(snap.lt, 0, readyAt < 0 ? 1e12 : readyAt + 2500),
    api_requests_load: net.api.length,
    js_bytes: net.js,
    commits_load: snap.commits,
  };
  await page.close();
  return r;
}

/** Warm SPA navigation: start on `from`, click a link / navigate to `key`. */
async function warmNav(page, key) {
  const s = SURFACES[key];
  const before = await snapshot(page);
  const t0 = await page.evaluate(() => performance.now());
  // Client-side navigation through the router, like clicking a rail link.
  const clicked = await page.evaluate((path) => {
    const a = [...document.querySelectorAll("a[href]")].find(
      (el) => new URL(el.href).pathname === path,
    );
    if (a) {
      a.click();
      return true;
    }
    return false;
  }, s.path);
  if (!clicked) {
    await page.evaluate((path) => {
      window.history.pushState({}, "", path);
      window.dispatchEvent(new PopStateEvent("popstate"));
    }, s.path);
  }
  const readyAt = await page.evaluate((src) => window.__waitFor(src, 15000), s.ready);
  await sleep(1500);
  const after = await snapshot(page);
  return {
    via: clicked ? "link" : "history",
    ready_ms: readyAt < 0 ? null : Math.round(readyAt - t0),
    lt: ltStats(after.lt, t0, after.now),
    commits: after.commits - before.commits,
    fibers: after.fibers - before.fibers,
  };
}

async function idle(page, net, ms) {
  const startTs = Date.now() / 1000;
  const before = await snapshot(page);
  const apiBefore = net.api.length;
  const wsBefore = net.ws;
  const wsBytesBefore = net.wsBytes;
  await sleep(ms);
  const after = await snapshot(page);
  const api = net.api.slice(apiBefore);
  const byPath = {};
  for (const a of api) byPath[a.path] = (byPath[a.path] || 0) + 1;
  const mins = ms / 60000;
  return {
    start_ts: startTs,
    end_ts: Date.now() / 1000,
    api_per_min: +(api.length / mins).toFixed(1),
    ws_frames_per_min: +((net.ws - wsBefore) / mins).toFixed(1),
    ws_kb_per_min: +((net.wsBytes - wsBytesBefore) / 1024 / mins).toFixed(1),
    lt: ltStats(after.lt, before.now, after.now),
    commits_per_min: +((after.commits - before.commits) / mins).toFixed(1),
    fibers_per_min: Math.round((after.fibers - before.fibers) / mins),
    by_path: Object.fromEntries(Object.entries(byPath).sort((a, b) => b[1] - a[1])),
  };
}

/** Click something and time until `done` holds, plus the Event Timing entry. */
async function interact(page, label, act, done, loaded) {
  const before = await snapshot(page);
  const t0 = await page.evaluate(() => performance.now());
  await act();
  const [readyAt, loadedAt] = await page.evaluate((d, l) => Promise.all([
    window.__waitFor(d, 10000), l ? window.__waitFor(l, 10000) : Promise.resolve(-1),
  ]), done, loaded ?? null);
  await sleep(800);
  const after = await snapshot(page);
  return {
    label,
    visible_ms: readyAt < 0 ? null : Math.round(readyAt - t0),
    loaded_ms: loaded ? (loadedAt < 0 ? null : Math.round(loadedAt - t0)) : undefined,
    inp: interaction(after.ev, t0),
    lt: ltStats(after.lt, t0, after.now),
    commits: after.commits - before.commits,
    fibers: after.fibers - before.fibers,
  };
}

async function interactions(browser) {
  const out = [];
  const { page } = await newPage(browser);
  await page.goto(BASE + SURFACES.tasks.path, { waitUntil: "domcontentloaded" });
  await page.evaluate((src) => window.__waitFor(src, 20000), SURFACES.tasks.ready);
  await sleep(3000);

  // Open a task from the list into the pane.
  {
    const title = await page.evaluate((i) => { const r = document.querySelectorAll("[data-task-row]"); return r[Math.min(i, r.length - 1)].querySelector("td span")?.innerText.trim(); }, 3);
    out.push(
      await interact(
        page,
        "tasks: click task row → pane",
        async () => {
          const box = await (await page.$$("[data-task-row]"))[3].$("td span").then((el) => el.boundingBox());
          await page.mouse.click(box.x + Math.min(40, box.width / 2), box.y + box.height / 2);
        },
        `() => { const h = document.querySelector('[data-testid="shell-pane-scroller"] h2'); return !!h && h.innerText.trim() === ${JSON.stringify(title)}; }`,
        `() => { const s = document.querySelector('[data-testid="shell-pane-scroller"]'); const h = s && s.querySelector('h2'); return !!h && h.innerText.trim() === ${JSON.stringify(title)} && /attachments/i.test(s.innerText); }`,
      ),
    );
  }
  await sleep(2000);
  // Click a different task while the pane is open.
  {
    const title = await page.evaluate((i) => { const r = document.querySelectorAll("[data-task-row]"); return r[Math.min(i, r.length - 1)].querySelector("td span")?.innerText.trim(); }, 7);
    out.push(
      await interact(
        page,
        "tasks: click another task (pane open)",
        async () => {
          const box = await (await page.$$("[data-task-row]"))[7].$("td span").then((el) => el.boundingBox());
          await page.mouse.click(box.x + Math.min(40, box.width / 2), box.y + box.height / 2);
        },
        `() => { const h = document.querySelector('[data-testid="shell-pane-scroller"] h2'); return !!h && h.innerText.trim() === ${JSON.stringify(title)}; }`,
        `() => { const s = document.querySelector('[data-testid="shell-pane-scroller"]'); const h = s && s.querySelector('h2'); return !!h && h.innerText.trim() === ${JSON.stringify(title)} && /attachments/i.test(s.innerText); }`,
      ),
    );
  }
  if (args.includes("--task-detail-only")) {
    await page.close();
    return out;
  }
  await sleep(1500);
  await page.keyboard.press("Escape");
  await sleep(1500);

  // Type into search: one keystroke → filtered list.
  await page.click('input[aria-label="Search tasks"]');
  const nBefore = await page.evaluate(() => document.querySelector('[aria-label="Task list"] p')?.innerText);
  out.push(
    await interact(
      page,
      "tasks: type 'x' in search",
      async () => {
        await page.keyboard.type("x");
      },
      `() => document.querySelector('[aria-label="Task list"] p')?.innerText !== ${JSON.stringify(nBefore)}`,
    ),
  );
  await sleep(500);
  out.push(
    await interact(
      page,
      "tasks: clear search (Backspace)",
      async () => {
        await page.keyboard.press("Backspace");
      },
      `() => document.querySelector('[aria-label="Task list"] p')?.innerText === ${JSON.stringify(nBefore)}`,
    ),
  );
  await sleep(1000);

  // Switch tab: tasks → graph → tasks.
  out.push(
    await interact(
      page,
      "tab: tasks → graph",
      async () => {
        await page.evaluate(() =>
          [...document.querySelectorAll('nav[aria-label="Command Center views"] a')]
            .find((a) => /graph$/.test(new URL(a.href).pathname))
            .click(),
        );
      },
      SURFACES.graph.ready,
    ),
  );
  await sleep(2500);
  out.push(
    await interact(
      page,
      "tab: graph → tasks",
      async () => {
        await page.evaluate(() =>
          [...document.querySelectorAll('nav[aria-label="Command Center views"] a')]
            .find((a) => /tasks$/.test(new URL(a.href).pathname))
            .click(),
        );
      },
      SURFACES.tasks.ready,
    ),
  );
  await sleep(1500);
  // Graph: click a node → pane.
  await page.evaluate(() =>
    [...document.querySelectorAll('nav[aria-label="Command Center views"] a')]
      .find((a) => /graph$/.test(new URL(a.href).pathname))
      .click(),
  );
  await page.evaluate((src) => window.__waitFor(src, 15000), SURFACES.graph.ready);
  await sleep(2500);
  out.push(
    await interact(
      page,
      "graph: click task node → pane",
      async () => {
        const nodes = await page.$$(".react-flow__node");
        for (const n of nodes) {
          const box = await n.boundingBox();
          if (box && box.x > 300 && box.x < 1100 && box.y > 150 && box.y < 900) {
            await page.mouse.click(box.x + box.width / 2, box.y + 12);
            return;
          }
        }
      },
      `() => { const h = document.querySelector('[data-testid="shell-pane-scroller"] h2'); return !!h && h.innerText.trim() !== "" && !/Loading…/.test(h.innerText); }`,
      `() => { const s = document.querySelector('[data-testid="shell-pane-scroller"]'); return !!s && /attachments/i.test(s.innerText); }`,
    ),
  );
  await sleep(1500);
  await page.keyboard.press("Escape");
  await sleep(1000);

  // Reviews: change the state filter.
  await page.evaluate(() => [...document.querySelectorAll("a[href]")].find((a) => new URL(a.href).pathname === "/reviews")?.click());
  await page.evaluate((src) => window.__waitFor(src, 15000), SURFACES.reviews.ready);
  await sleep(2000);
  const reviewTarget = await page.evaluate(() => {
    const sel = document.querySelector('select[aria-label="Review state"]');
    return [...sel.options].map((o) => o.value).find((v) => v !== sel.value);
  });
  out.push(
    await interact(
      page,
      "reviews: change state filter",
      async () => {
        await page.select('select[aria-label="Review state"]', reviewTarget);
      },
      `() => document.querySelector('select[aria-label="Review state"]').value === ${JSON.stringify(reviewTarget)} && !/Loading reviews…/.test(document.querySelector("main").innerText)`,
    ),
  );
  await sleep(1000);

  // Metrics: switch time range.
  await page.evaluate(() => [...document.querySelectorAll("a[href]")].find((a) => new URL(a.href).pathname === "/metrics")?.click());
  await page.evaluate((src) => window.__waitFor(src, 15000), SURFACES.metrics.ready);
  await sleep(2500);
  out.push(
    await interact(
      page,
      "metrics: switch time range",
      async () => {
        const btns = await page.$$('[aria-label="Time range"] button');
        const pressed = await Promise.all(btns.map((b) => b.evaluate((e) => e.getAttribute("aria-pressed"))));
        const idx = pressed.findIndex((p) => p !== "true");
        await btns[idx].click();
      },
      `() => [...document.querySelectorAll('[aria-label="Time range"] button')].some((b, i) => b.getAttribute('aria-pressed') === 'true') && ${noLoading}()`,
    ),
  );
  await page.close();
  return out;
}

async function main() {
  const { default: puppeteer } = await import("puppeteer-core");
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME ?? "/usr/bin/google-chrome",
    headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1600,1000"],
  });
  const surfaces = ONLY?.length ? ONLY : Object.keys(SURFACES);
  try {
    const res = { base: BASE, cpu: CPU, idle_ms: IDLE_MS, runs: RUNS, cold: {}, warm: {}, idle: {}, interactions: [] };

    res.manifest = {
      chrome: await browser.version(), viewport: [1600, 1000], warmup_ms: WARMUP_MS,
      observe_ms: IDLE_MS, runs: RUNS, clients: CLIENTS, api: API || null,
    };
    res.start_ts = Date.now() / 1000;

    for (const key of surfaces) {
      const runs = [];
      for (let i = 0; i < RUNS; i++) runs.push(await coldLoad(browser, key));
      res.cold[key] = {
        ready_ms: median(runs.map((r) => r.ready_ms)),
        lt_count: median(runs.map((r) => r.lt_load.count)),
        tbt: median(runs.map((r) => r.lt_load.tbt)),
        api_requests: median(runs.map((r) => r.api_requests_load)),
        js_kb: Math.round(median(runs.map((r) => r.js_bytes)) / 1024),
        commits: median(runs.map((r) => r.commits_load)),
        script_ms: median(runs.map((r) => r.script_ms)),
        task_ms: median(runs.map((r) => r.task_ms)),
        raw: runs.map((r) => r.ready_ms),
        raw_samples: runs,
      };
      console.error("cold", key, JSON.stringify(res.cold[key]));
    }

    // Warm navigation: cycle through the surfaces in one SPA session.
    {
      const { page } = await newPage(browser);
      await page.goto(BASE + SURFACES.tasks.path, { waitUntil: "domcontentloaded" });
      await page.evaluate((src) => window.__waitFor(src, 20000), SURFACES.tasks.ready);
      await sleep(3000);
      const order = surfaces.filter((k) => k !== "tasks").concat(["tasks"]);
      const all = {};
      for (let i = 0; i < RUNS; i++) {
        for (const key of order) (all[key] ??= []).push(await warmNav(page, key));
      }
      for (const [key, runs] of Object.entries(all)) {
        res.warm[key] = {
          ready_ms: median(runs.map((r) => r.ready_ms)),
          tbt: median(runs.map((r) => r.lt.tbt)),
          lt_count: median(runs.map((r) => r.lt.count)),
          commits: median(runs.map((r) => r.commits)),
          fibers: median(runs.map((r) => r.fibers)),
          via: runs[0].via,
          raw: runs.map((r) => r.ready_ms),
          raw_samples: runs,
        };
        console.error("warm", key, JSON.stringify(res.warm[key]));
      }
      await page.close();
    }

    if (!process.argv.includes("--no-interactions")) {
      for (let i = 0; i < RUNS; i++) {
        try {
          res.interactions.push(await interactions(browser));
        } catch (e) {
          (res.interaction_errors ??= []).push(String(e));
          console.error("interactions failed", e);
        }
      }
      console.error("interactions", JSON.stringify(res.interactions.at(-1)));
    }

    if (IDLE_MS > 0) {
      for (const key of surfaces) {
        const pages = [];
        try {
          await Promise.all(Array.from({ length: CLIENTS }, async () => {
            const client = await newPage(browser, CLIENTS > 1);
            pages.push(client);
            await client.page.goto(BASE + SURFACES[key].path, { waitUntil: "domcontentloaded" });
            const ready = await client.page.evaluate((src) => window.__waitFor(src, 20000), SURFACES[key].ready);
            if (ready < 0) throw new Error(`idle surface ${key} did not become ready`);
            await sleep(WARMUP_MS);
          }));
          const clients = await Promise.all(pages.map(({ page, net }) => idle(page, net, IDLE_MS)));
          const aggregate = { clients };
          for (const field of ["api_per_min", "ws_frames_per_min", "ws_kb_per_min", "commits_per_min", "fibers_per_min"])
            aggregate[field] = median(clients.map((c) => c[field]));
          aggregate.lt = Object.fromEntries(["count", "tbt", "max"].map((field) => [field, median(clients.map((c) => c.lt[field]))]));
          aggregate.by_path = {};
          for (const c of clients)
            for (const [path, count] of Object.entries(c.by_path))
              aggregate.by_path[path] = (aggregate.by_path[path] ?? 0) + count;
          res.idle[key] = aggregate;
          console.error("idle", key, JSON.stringify(aggregate));
        } finally {
          await Promise.all(pages.map(({ page }) => page.close()));
        }
      }
    }
    if (API) {
      res.api_start_ts = Date.now() / 1000;
      res.api = await probeApi(API, { project: PROJECT });
      res.api_end_ts = Date.now() / 1000;
    }
    res.end_ts = Date.now() / 1000;
    writeFileSync(OUT, JSON.stringify(res, null, 2));
  } finally {
    await browser.close();
  }
}
if (!BASE || !OUT || args.includes("--help")) {
  console.error("Usage: node harness.mjs <baseUrl> <outJson> [--api URL] [--clients N] [--warmup-ms N] [--observe-ms N] [--idle-ms N] [--runs N] [--cpu N] [--only a,b] [--project id] [--no-interactions] [--task-detail-only]");
  process.exit(args.includes("--help") ? 0 : 2);
}
if (!Number.isInteger(CLIENTS) || CLIENTS < 1 || !Number.isInteger(RUNS) || RUNS < 1 ||
    !Number.isFinite(WARMUP_MS) || WARMUP_MS < 0 || !Number.isFinite(IDLE_MS) || IDLE_MS < 0 ||
    !Number.isFinite(CPU) || CPU < 1 || ONLY.some((key) => !SURFACES[key])) {
  console.error("Invalid clients, runs, duration, CPU rate or surface");
  process.exit(2);
}
main().catch((e) => {
  console.error(e);
  process.exit(1);
});
