import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  listAgents, getAgent, createAgent, editAgent, deleteAgent, startAgentTerminal,
  type AgentSummary, type ListAgentsResponse, type CreateAgentRequest, type EditAgentRequest, type DeleteAgentRequest, type StartAgentTerminalRequest,
} from "./client";

import { useIntelligenceClasses } from "./hooks";

export type FlockAgent = AgentSummary;
export type AgentSettings = NonNullable<FlockAgent["settings"]>;

// One request feeds both the roster and its sub-agent rollup. Splitting them
// into two queries would let the header and the rows disagree about the same
// poll — the total is derived from these exact rows, server-side.
//
// The roster is live through the event stream: every agent.*, session.*,
// task.* and message.* frame refreshes it (coalesced, ws/useEventStream.ts),
// which covers assignments, state, questions and session turnover. The poll
// is only reconciliation. It used to run every 5s from the always-mounted
// shell rail, and the roster is one of the daemon's most expensive reads
// (0.7s alone, several seconds under load, blocking other requests while it
// runs), so each open dashboard kept the daemon busy and every click waited.
const flockQuery = {
  queryKey: ["agents", "flock"],
  queryFn: async () => {
    const { data } = await listAgents({ body: {}, throwOnError: true });
    return data;
  },
  staleTime: 2_000,
  refetchInterval: 30_000,
} as const;

export function useAgentFlock() {
  return useQuery({ ...flockQuery, select: (data) => data.agents ?? [] });
}

/** Active native + AQ sub-agents across the whole flock (and per profile). */
export function useFlockSubagents() {
  return useQuery({ ...flockQuery, select: (data) => data.subagents ?? null });
}

export function useFlockAgent(agentId: string) {
  return useQuery({
    queryKey: ["agents", "detail", agentId],
    queryFn: async () => (await getAgent({ body: { agent_id: agentId }, throwOnError: true })).data,
    enabled: !!agentId,
    refetchInterval: 30_000,
  });
}

export function useEditAgent() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: EditAgentRequest) =>
      (await editAgent({ body: input, throwOnError: true })).data,
    onSuccess: (agent) => {
      client.setQueryData<ListAgentsResponse>(["agents", "flock"], (data) => data && ({
        ...data, agents: (data.agents ?? []).map((row) => row.id === agent.id ? agent : row),
      }));
      client.setQueryData(["agents", "detail", agent.id], agent);
      void client.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useCreateAgent() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateAgentRequest) =>
      (await createAgent({ body: input, throwOnError: true })).data,
    onSuccess: () => { void client.invalidateQueries({ queryKey: ["agents"] }); },
  });
}

export function useStartAgentTerminal() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: StartAgentTerminalRequest) =>
      (await startAgentTerminal({ body: input, throwOnError: true })).data,
    retry: false,
    onSuccess: (agent) => {
      client.setQueryData<ListAgentsResponse>(["agents", "flock"], (data) => data && ({
        ...data, agents: (data.agents ?? []).map((row) => row.id === agent.id ? agent : row),
      }));
      client.setQueryData(["agents", "detail", agent.id], agent);
      void client.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useDeleteAgent() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: DeleteAgentRequest) =>
      (await deleteAgent({ body: input, throwOnError: true })).data,
    onSuccess: (_data, input) => {
      client.setQueryData<ListAgentsResponse>(["agents", "flock"], (data) => data && ({
        ...data, agents: (data.agents ?? []).filter((row) => row.id !== input.agent_id),
      }));
      client.removeQueries({ queryKey: ["agents", "detail", input.agent_id], exact: true });
      void client.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useAgentIntelligenceClasses() {
  return useIntelligenceClasses();
}
