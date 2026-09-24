import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { DashboardStateProvider } from "./api/DashboardStateProvider";
import { EventStreamProvider } from "./ws/EventStreamProvider";
import { BrowserHistoryContext } from "./shell/historyState";
import { PANE_REGISTRY } from "./panes/registry";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
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

// Pane views are code-split. The task pane is what a click on any task opens,
// so fetch its chunk once the first screen is up rather than on that click.
const whenIdle = window.requestIdleCallback ?? ((run: () => void) => window.setTimeout(run, 1500));
whenIdle(() => {
  void PANE_REGISTRY["task-detail"]?.preload?.();
});
