import type { FlockAgent } from "../../api/agents";

export function AgentSubagents({ agent }: { agent: FlockAgent }) {
  const complete = agent.subagent_count_complete;
  const count = agent.active_subagent_count;
  const known = count ?? agent.aq_subagent_count ?? 0;
  const label = complete && count != null
    ? count + " active sub-agents"
    : known > 0 ? known + "+ active sub-agents" : "Sub-agents unknown";
  const title = complete
    ? "Active direct AQ and native sub-agents. Completed and queued work is excluded."
    : "Partial count: native sub-agent telemetry is incomplete or unavailable. "
      + (agent.aq_subagent_count ?? 0) + " active AQ sub-agents are known; the total may be higher.";
  return <span title={title}>{label}</span>;
}

export function AgentState({ agent }: { agent: FlockAgent }) {
  return agent.state || "Idle";
}

export function AgentEligibility({ agent }: { agent: FlockAgent }) {
  return agent.enabled === false ? (
    <span className="block text-[10px] text-amber-400" title="Disabled for new work; current tasks and sessions continue.">
      New work disabled
    </span>
  ) : null;
}
