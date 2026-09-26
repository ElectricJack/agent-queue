// Direct read probes matching the dashboard surfaces, without browser/relay cost.
import { pathToFileURL } from "node:url";

export const routes = (project, taskId) => [
  { label: "GET /health", method: "GET", path: "/health" },
  { label: "GET /ready", method: "GET", path: "/ready" },
  { label: "GET /api/metrics/series", method: "GET", path: () => {
    const now = Date.now() / 1000;
    return `/api/metrics/series?from=${now - 60}&to=${now}&step=1s`;
  } },
  // ListTasksRequest has no limit; this intentionally measures the real list.
  { label: "POST /api/task/list", method: "POST", path: "/api/task/list", body: { project_id: project } },
  { label: "POST /api/agent/list", method: "POST", path: "/api/agent/list", body: { project_id: project } },
  { label: "POST /api/pool/status", method: "POST", path: "/api/pool/status", body: { project_id: project } },
  { label: "POST /api/task/gate-list", method: "POST", path: "/api/task/gate-list", body: { project_id: project } },
  { label: "POST /api/task/get", method: "POST", path: "/api/task/get", body: { task_id: taskId } },
];

export async function probeApi(base, { project = "agent-queue", samples = 30, timeoutMs = 15000 } = {}) {
  if (!Number.isInteger(samples) || samples < 1) throw new Error("samples must be positive");
  base = base.replace(/\/$/, "");
  let taskId = null;
  try {
    const first = await fetch(`${base}/api/task/list`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ project_id: project }), signal: AbortSignal.timeout(timeoutMs),
    });
    if (first.ok) taskId = (await first.json())?.tasks?.[0]?.id ?? null;
  } catch {}
  const out = {};
  for (const route of routes(project, taskId)) {
    if (route.path === "/api/task/get" && !taskId) {
      out[route.label] = { raw_ms: [], median_ms: null, p95_ms: null, errors: 0, reason: "no_task_id" };
      continue;
    }
    const raw = [];
    let errors = 0;
    for (let i = 0; i < samples; i++) {
      const path = typeof route.path === "function" ? route.path() : route.path;
      const t0 = performance.now();
      try {
        const res = await fetch(base + path, {
          method: route.method, headers: { "content-type": "application/json" },
          body: route.body ? JSON.stringify(route.body) : undefined,
          signal: AbortSignal.timeout(timeoutMs),
        });
        await res.arrayBuffer(); // Include response-body delivery, as the browser does.
        if (!res.ok) errors++;
      } catch { errors++; }
      raw.push(performance.now() - t0);
    }
    const sorted = [...raw].sort((a, b) => a - b);
    const mid = Math.floor(sorted.length / 2);
    out[route.label] = {
      raw_ms: raw,
      median_ms: sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2,
      p95_ms: sorted[Math.ceil(sorted.length * 0.95) - 1], errors,
    };
  }
  return out;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [base, project] = process.argv.slice(2);
  if (!base || base === "--help") {
    console.error("Usage: node api.mjs <daemonUrl> [project]");
    process.exitCode = base === "--help" ? 0 : 2;
  } else {
    console.log(JSON.stringify(await probeApi(base, { project }), null, 2));
  }
}
