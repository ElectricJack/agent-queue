import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { legacyFetch } from "../../api/legacy-fetch";
import { useGates, useResolveGate, type GateSummary } from "../../api/hooks";

export interface ProposalTask {
  tempId: string;
  title: string;
  description: string;
  priority?: number;
}

export interface ProposalEdge {
  from: string;
  to: string;
  dep_type: string;
}

export interface ProposalDetail {
  proposal_id: string;
  project_id: string;
  source: string;
  tasks: ProposalTask[];
  edges: ProposalEdge[];
  status: "draft" | "ready" | "committed" | "discarded";
}

/** GET /api/proposals/{proposalId} — not in the generated SDK (bare dict
 *  response, no registered Pydantic model). Follows the same legacyFetch
 *  pattern already established by GhostOverlay.tsx for this endpoint. */
export function useProposal(proposalId: string) {
  return useQuery<ProposalDetail>({
    queryKey: ["proposal", proposalId],
    enabled: !!proposalId,
    queryFn: async () => {
      const r = await legacyFetch(`/api/proposals/${proposalId}`);
      if (r.status === 404) throw new Error("proposal not found");
      if (!r.ok) throw new Error(`proposal fetch ${r.status}`);
      return (await r.json()) as ProposalDetail;
    },
    refetchInterval: (query) => (query.state.data?.status === "ready" ? 15_000 : false),
  });
}

/** There is no GET /api/proposals/{id}/gate endpoint — the default-pipeline
 *  creates the review gate with subject_id === proposalId (routing gate),
 *  so the pane locates it by filtering the existing open-gates list. */
export function useProposalGate(projectId: string | undefined, proposalId: string) {
  const gatesQuery = useGates({ projectId, status: "open", enabled: !!projectId });
  const gate = (gatesQuery.data ?? []).find((g: GateSummary) => {
    const subjectId = (g as unknown as { subject_id?: string }).subject_id;
    return g.gate_type === "routing" && subjectId === proposalId;
  });
  return { ...gatesQuery, gate };
}

export { useResolveGate };

export function useDiscardProposal(proposalId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const r = await legacyFetch(`/api/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          command: "task_batch_discard",
          args: { proposal_id: proposalId },
        }),
      });
      const body = (await r.json()) as { ok?: boolean; error?: string };
      if (!r.ok || body.ok === false) {
        throw new Error(body.error ?? `discard failed: ${r.status}`);
      }
      return body;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["proposal", proposalId] });
    },
  });
}
