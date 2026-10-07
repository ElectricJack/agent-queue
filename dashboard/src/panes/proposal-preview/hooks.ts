import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getProposalApiProposalsProposalIdGet, taskBatchDiscard } from "../../api/client";
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
  edits?: Array<Record<string, unknown> & { task_id: string }>;
  remove_edges?: ProposalEdge[];
  comments?: Array<{ task_id: string; body: string; kind?: string }>;
  diff?: {
    tasks: Array<{
      task_id: string;
      before: Record<string, unknown> | null;
      after: Record<string, unknown> | null;
    }>;
  } | null;
  status: "draft" | "ready" | "committed" | "discarded";
}

/** Proposal operations and their validated before/after diff. */
export function useProposal(proposalId: string) {
  return useQuery<ProposalDetail>({
    queryKey: ["proposal", proposalId],
    enabled: !!proposalId,
    queryFn: async () => {
      const { data } = await getProposalApiProposalsProposalIdGet({
        path: { proposal_id: proposalId },
      });
      return data as unknown as ProposalDetail;
    },
    refetchInterval: (query) => (query.state.data?.status === "ready" ? 15_000 : false),
  });
}

/** The pipeline's human gate awaits this exact proposal. */
export function useProposalGate(projectId: string | undefined, proposalId: string) {
  const gatesQuery = useGates({ projectId, status: "open", enabled: !!projectId });
  const gate = (gatesQuery.data ?? []).find((g: GateSummary) => {
    return g.gate_type === "human" && g.await_id === proposalId;
  });
  return { ...gatesQuery, gate };
}

export { useResolveGate };

export function useDiscardProposal(proposalId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data } = await taskBatchDiscard({ body: { proposal_id: proposalId } });
      if (data?.success === false) throw new Error("discard failed");
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["proposal", proposalId] });
    },
  });
}
