import { useQuery } from "@tanstack/react-query";
import { recordCapabilities } from "./client";

export function useKnowledgeCapabilities(projectId: string | undefined) {
  return useQuery({
    queryKey: ["knowledge-capabilities", projectId],
    queryFn: async () => {
      const { data } = await recordCapabilities({ body: { project_id: projectId } });
      const caps = data?.capabilities ?? {};
      return {
        available: caps.enabled === true && caps.ui_enabled === true
          && Array.isArray(caps.enabled_projects) && caps.enabled_projects.includes(projectId),
        writes: caps.writes_enabled === true && caps.legacy_memory_mode === "disabled",
        operations: Array.isArray(caps.granted_operations) ? caps.granted_operations as string[] : [],
      };
    },
    enabled: !!projectId,
    staleTime: 15_000,
  });
}
