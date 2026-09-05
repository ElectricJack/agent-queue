import { useCallback, useContext } from "react";
import { QueryClientContext } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import type { OnboardingResult } from "./state";

/**
 * Design §4.6 — after a successful onboarding: invalidate the project and
 * workspace queries, run the caller's hook (the rail uses it to expand
 * Projects), then open the new project's overview.
 *
 * Reads the QueryClient through its context rather than `useQueryClient` so a
 * shell rendered without a provider (some navigation tests) still mounts.
 */
export function useProjectCreatedNavigation(beforeNavigate?: () => void) {
  const navigate = useNavigate();
  const queryClient = useContext(QueryClientContext);
  return useCallback(
    (result: OnboardingResult) => {
      void queryClient?.invalidateQueries({ queryKey: ["projects"] });
      void queryClient?.invalidateQueries({ queryKey: ["workspaces"] });
      beforeNavigate?.();
      navigate(`/projects/${encodeURIComponent(result.project_id)}/overview`);
    },
    [queryClient, navigate, beforeNavigate],
  );
}
