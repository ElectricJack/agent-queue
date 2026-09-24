import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { DashboardStateProvider } from "./api/DashboardStateProvider";
import { EventStreamProvider } from "./ws/EventStreamProvider";
import { BrowserHistoryContext } from "./shell/historyState";
import { PANE_REGISTRY } from "./panes/registry";
import { preloadWorkspaceViews } from "./routeChunks";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Live data reaches the cache through the event stream (which keeps
      // invalidating while the tab is hidden) and each query's own poll, so a
      // read younger than this is current. At 10s — shorter than nearly every
      // poll — each return to the tab and each revisit of a page refetched
      // every mounted query at once, ahead of whatever the user clicked next.
      staleTime: 30_000,
      refetchOnWindowFocus: true,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename={import.meta.env.BASE_URL}>
        <BrowserHistoryContext.Provider value={window.history}>
          <EventStreamProvider>
            <DashboardStateProvider>
              <App />
            </DashboardStateProvider>
          </EventStreamProvider>
        </BrowserHistoryContext.Provider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);

// Pane views and routes are code-split. Fetch the chunks the next click most
// likely needs — the task pane any task opens, and the project workspace's
// graph and task list — once the first screen is up, not on that click. The
// main thread is idle while the first screen waits on the daemon, so an idle
// callback alone would run then and put the chunks' download and evaluation
// in the first screen's path; wait a moment past load first.
const whenIdle = window.requestIdleCallback ?? ((run: () => void) => window.setTimeout(run, 0));
const preloadNextViews = () => window.setTimeout(() => whenIdle(() => {
  void PANE_REGISTRY["task-detail"]?.preload?.();
  preloadWorkspaceViews();
}), 2_000);
if (document.readyState === "complete") preloadNextViews();
else window.addEventListener("load", preloadNextViews, { once: true });
