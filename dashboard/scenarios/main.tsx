/* Playbook V2 §13.3 manual-scenario harness.
 *
 * Mounts the real `PlaybookSemanticReview` surface against payloads produced by
 * `scripts/pkg5_scenarios/build_payloads.py` — the actual `project_graph`,
 * `diff_artifacts`, `project_overlay` and `evaluate_health` output for the §10
 * `review-pipeline` artifacts. Only the transport is replaced: every component,
 * hook and DTO on screen is the production one.
 *
 * Dev only. Never bundled into the dashboard app (`index.html` does not
 * reference it) and never pointed at a daemon.
 */
import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import PlaybookSemanticReview from "../src/pages/playbook-graph-v2/PlaybookSemanticReview";
import "../src/index.css";

type Payloads = Record<string, unknown>;

/** The real transport, captured before it is replaced, so reloading a scenario
 *  file still works after `installTransport` has run (StrictMode re-runs it). */
const nativeFetch = window.fetch.bind(window);

const PLAYBOOK_ID = "review-pipeline";

function scenarioName(): string {
  const requested = new URLSearchParams(window.location.search).get("scenario");
  return requested ?? "01-branching";
}

const GRAPH_URL = "/api/playbook/v2-graph";
const DIFF_URL = "/api/playbook/artifact-diff";

/** Answer every `/api/...` call from the scenario file; refuse anything else so
 *  a missing payload fails loudly instead of silently hitting the vite proxy.
 *
 *  Two endpoints are keyed rather than fixed, because the review surface
 *  refetches them as an operator works: the graph by artifact hash then event
 *  scope, and the diff by the candidate being reviewed. Serving one frozen
 *  response for either would make the chooser and the scope filter look inert.
 */
function installTransport(payloads: Payloads) {
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = new URL(url, window.location.origin).pathname;
    // The generated client hands `fetch` a `Request`, so the body is on the
    // request rather than in `init` — read whichever carries it.
    let raw = init?.body as string | undefined;
    if (raw === undefined && typeof input === "object" && "clone" in input) {
      raw = await (input as Request).clone().text();
    }
    let body: Record<string, unknown> = {};
    try {
      body = JSON.parse(raw ?? "{}") as Record<string, unknown>;
    } catch {
      body = {};
    }

    const json = (value: unknown, status = 200) =>
      new Response(JSON.stringify(value), {
        status,
        headers: { "content-type": "application/json" },
      });

    if (path === GRAPH_URL) {
      const byArtifact = payloads[GRAPH_URL] as Record<string, Record<string, unknown>>;
      const scope = (body.event_type as string) ?? "";
      const graph = byArtifact?.[(body.artifact_sha256 as string) ?? "active"]?.[scope];
      return graph
        ? json(graph)
        : json({ error: `no graph payload for ${String(body.artifact_sha256)} / "${scope}"` }, 404);
    }

    if (path === DIFF_URL) {
      const byTarget = payloads[DIFF_URL] as Record<string, unknown> | null;
      const diff = byTarget?.[body.target_sha256 as string];
      return diff
        ? json(diff)
        : json({ error: `no diff payload for ${String(body.target_sha256)}` }, 404);
    }

    if (!(path in payloads)) {
      return json({ error: `no scenario payload for ${path}` }, 501);
    }
    const payload = payloads[path];
    if (payload === null) {
      return json({ error: `payload for ${path} is null in this scenario` }, 404);
    }
    return json(payload);
  };
}

function Harness() {
  const name = scenarioName();
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void (async () => {
      try {
        const response = await nativeFetch(`/scenarios/payloads/${name}.json`);
        if (!response.ok) throw new Error(`${response.status} loading ${name}.json`);
        installTransport((await response.json()) as Payloads);
        setReady(true);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    })();
  }, [name]);
  if (error) return <p className="p-6 text-sm text-red-300">{error}</p>;
  if (!ready) return <p className="p-6 text-sm text-gray-400">Loading scenario {name}…</p>;
  return (
    <div className="min-h-screen space-y-4 p-6">
      <h1 className="text-sm font-medium text-gray-400" data-testid="scenario-name">
        §13.3 scenario — {name}
      </h1>
      <PlaybookSemanticReview playbookId={PLAYBOOK_ID} />
    </div>
  );
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <Harness />
    </QueryClientProvider>
  </StrictMode>,
);
