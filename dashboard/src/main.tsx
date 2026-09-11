import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { DashboardStateProvider } from "./api/DashboardStateProvider";
import { EventStreamProvider } from "./ws/EventStreamProvider";
import { BrowserHistoryContext } from "./shell/historyState";
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
