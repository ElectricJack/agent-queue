import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { EventStreamProvider } from "./ws/EventStreamProvider";
import { DashboardStateProvider } from "./state/dashboardState";
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
      <DashboardStateProvider>
        <BrowserRouter basename={import.meta.env.BASE_URL}>
          <EventStreamProvider>
            <App />
          </EventStreamProvider>
        </BrowserRouter>
      </DashboardStateProvider>
    </QueryClientProvider>
  </StrictMode>,
);
