import { createContext, useContext } from "react";

export type DashboardStateStatus = "loading" | "ready" | "unavailable";

export interface DashboardStateContextValue {
  ownerId: string | null;
  status: DashboardStateStatus;
}

export const DashboardStateContext = createContext<DashboardStateContextValue>({
  ownerId: null,
  status: "loading",
});

export function useDashboardStateStatus(): DashboardStateContextValue {
  return useContext(DashboardStateContext);
}

