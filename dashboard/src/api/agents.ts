import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  listAgents, getAgent, createAgent, editAgent, deleteAgent, startAgentTerminal,
  type AgentSummary, type CreateAgentRequest, type EditAgentRequest, type DeleteAgentRequest, type StartAgentTerminalRequest,
} from "./client";

import { useIntelligenceClasses } from "./hooks";

export type FlockAgent = AgentSummary;
export type AgentSettings = NonNullable<FlockAgent["settings"]>;

export function useAgentFlock() {
  return useQuery({
    queryKey: ["agents", "flock"],
    queryFn: async () => {
      const { data } = await listAgents({ body: {}, throwOnError: true });
      return data.agents ?? [];
    },
    staleTime: 2_000,
    refetchInterval: 5_000,
  });
}

export function useFlockAgent(agentId: string) {
  return useQuery({
    queryKey: ["agents", "detail", agentId],
    queryFn: async () => (await getAgent({ body: { agent_id: agentId }, throwOnError: true })).data,
    enabled: !!agentId,
    refetchInterval: 5_000,
  });
}

export function useEditAgent() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input: EditAgentRequest) =>
      (await editAgent({ body: input, throwOnError: true })).data,
    onSuccess: (agent) => {
      client.setQueryData<FlockAgent[]>(["agents", "flock"], (rows) => rows?.map((row) => row.id === agent.id ? agent : row));
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
      client.setQueryData<FlockAgent[]>(["agents", "flock"], (rows) => rows?.map((row) => row.id === agent.id ? agent : row));
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
      client.setQueryData<FlockAgent[]>(["agents", "flock"], (rows) => rows?.filter((row) => row.id !== input.agent_id));
      client.removeQueries({ queryKey: ["agents", "detail", input.agent_id], exact: true });
      void client.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useAgentIntelligenceClasses() {
  return useIntelligenceClasses();
}
