import type { ReactNode } from "react";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";

import { client } from "../client";
import { usePullRequestProjectSummaries } from "../pullRequests";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  client.setConfig({ baseUrl: "" });
});

it("fetches the project aggregate through the SDK without requesting individual PRs", async () => {
  const summary = {
    projects: [{
      project_id: "aq", project_name: "Agent Queue", repository: "acme/aq",
      open_pr_count: 24, pulls_url: "https://github.com/acme/aq/pulls",
    }],
    snapshot_at: 100, snapshot_age_seconds: 10,
  };
  const requests: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    requests.push(url.pathname);
    if (url.pathname !== "/api/reviews/pull-requests/summary") {
      throw new Error(`Unexpected request: ${url.pathname}`);
    }
    return new Response(JSON.stringify(summary), {
      headers: { "Content-Type": "application/json" },
    });
  }));
  client.setConfig({ baseUrl: "http://localhost" });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const { result, unmount } = renderHook(() => usePullRequestProjectSummaries(), { wrapper });
  await waitFor(() => expect(result.current.isSuccess).toBe(true));
  expect(result.current.data).toEqual(summary);
  expect(requests).toEqual(["/api/reviews/pull-requests/summary"]);
  unmount();
  queryClient.clear();
});
