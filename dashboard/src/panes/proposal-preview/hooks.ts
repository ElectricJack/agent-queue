import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getProposalApiProposalsProposalIdGet, getIngestBatch, gateShow, askPrepareDecision, askDecide } from "../../api/client";
import { useGates, type GateSummary } from "../../api/hooks";
import { randomUUID } from "../../randomUUID";

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
  status: "draft" | "ready" | "held" | "committed" | "discarded";
}

export function useIngestBatch(projectId: string | undefined, batchId: string, enabled = true) {
  return useQuery({
    queryKey: ["batch", projectId, batchId],
    enabled: enabled && !!projectId && !!batchId,
    queryFn: async () => (await getIngestBatch({
      path: { project_id: projectId!, batch_id: batchId }, throwOnError: true,
    })).data,
  });
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

/** Freeze a preview for the exact source version shown by the server. */
export function usePrepareProposalDecision() {
  return useMutation({
    mutationFn: async (input: { gate_id: string; option_key: "approve" | "decline" }) => {
      const { data: shown } = await gateShow({ body: { gate_id: input.gate_id } });
      const ask = shown?.gate.ask;
      if (!ask || ask.context_state !== "complete") throw new Error("This request needs more context.");
      const args = {
        ask_id: ask.id, option_key: input.option_key,
        expected: { decision_generation: ask.decision_generation,
          source_fingerprint: ask.source_fingerprint, feedback_version: ask.feedback_version },
      };
      const { data } = await askPrepareDecision({ body: args });
      if (!data?.success) throw new Error("The request changed; reload it before deciding.");
      const choices = ask.context.choices as Array<{ key: string; effect: string }>;
      return { args, preview_id: data.preview.id, idempotency_key: randomUUID(),
        effect: choices.find((choice) => choice.key === input.option_key)?.effect ?? "" };
    },
  });
}

export type ProposalDecisionPreview = NonNullable<ReturnType<typeof usePrepareProposalDecision>["data"]>;

export function useApplyProposalDecision(proposalId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (preview: ProposalDecisionPreview) => {
      const { data } = await askDecide({ body: { ...preview.args,
        preview_id: preview.preview_id, idempotency_key: preview.idempotency_key } });
      if (!data?.success || data.application_state !== "applied") {
        throw new Error("The decision has not finished applying. Reload to see its status.");
      }
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["proposal", proposalId] });
      queryClient.invalidateQueries({ queryKey: ["gates"] });
      queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}
