import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  listAgents, getAgent, createAgent, editAgent, listIntelligenceClasses,
  type AgentSummary, type CreateAgentRequest, type EditAgentRequest,
} from "./client";

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

export function useAgentIntelligenceClasses() {
  return useQuery({
    queryKey: ["intelligence-classes"],
    queryFn: async () => (await listIntelligenceClasses({ body: {}, throwOnError: true })).data,
    staleTime: 30_000,
  });
}
