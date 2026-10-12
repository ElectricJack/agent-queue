import type { SubagentRollup } from "../../api/client";
import type { FlockAgent } from "../../api/agents";

/** Native (harness-spawned) and AQ-delegated children, named separately.
 *
 * They are different things — a Claude `Task` sub-agent lives inside this
 * process, an AQ sub-agent is another worker holding its own task — so the
 * label says which is which instead of showing one opaque number.
 */
export function AgentSubagents({ agent }: { agent: FlockAgent }) {
  const complete = agent.subagent_count_complete;
  const aq = agent.aq_subagent_count ?? 0;
  const native = agent.native_subagent_count;
  const total = agent.active_subagent_count;
  const parts = [
    complete && native != null ? native + " native" : "native unknown",
    aq + " AQ",
  ].join(" · ");
  const label = complete && total != null
    ? total + " sub-agents (" + parts + ")"
    : aq + "+ sub-agents (" + parts + ")";
  const title = complete
    ? "Active sub-agents: " + (native ?? 0) + " spawned by the harness itself "
      + "(SubagentStart/SubagentStop hooks) and " + aq + " delegated as AQ tasks. "
      + "Completed and queued work is excluded. "
      + (agent.subagents_spawned_total ?? 0) + " native sub-agents spawned in total."
    : "Partial count: a live session was launched without its harness hooks, "
      + "so native sub-agents are unobserved. " + aq
      + " active AQ sub-agents are known; the total may be higher.";
  return <span title={title}>{label}</span>;
}

export function AgentState({ agent }: { agent: FlockAgent }) {
  return agent.waiting_question ? "Waiting for input" : agent.state || "Idle";
}

export function supervisorScope(agent: FlockAgent): string {
  return agent.project_id ? "Supervises " + agent.project_id : "Supervises all AQ projects";
}

export function SupervisorHealth({ agent }: { agent: FlockAgent }) {
  if (agent.role !== "supervisor") return null;
  const health = agent.auth_health;
  const state = health?.state ?? "unknown";
  const idle = agent.idle_seconds;
  const duration = idle == null ? "unknown" : idle < 60 ? Math.floor(idle) + "s"
    : idle < 3600 ? Math.floor(idle / 60) + "m" : Math.floor(idle / 3600) + "h";
  return <span className="block space-y-0.5">
    <span className="block">Idle: {duration} · Last activity: {agent.last_activity
      ? new Date(agent.last_activity * 1000).toLocaleString() : "unknown"}</span>
    <span className={"block " + (state === "failed" ? "text-red-400" : state === "healthy" ? "text-emerald-400" : "text-gray-500")}
      title={health?.observed_at ? "Last API auth outcome: HTTP " + health.status_code + " at "
        + new Date(health.observed_at * 1000).toLocaleString() : "No recent API auth observation"}>
      Auth: {state === "failed" ? "failed (HTTP " + health?.status_code + ")" : state}
    </span>
  </span>;
}

export function AgentEligibility({ agent }: { agent: FlockAgent }) {
  return agent.enabled === false ? (
    <span className="block text-[10px] text-amber-400" title="Disabled for new work; current tasks and sessions continue.">
      New work disabled
    </span>
  ) : null;
}


export function AgentWaitingQuestion({ agent }: { agent: FlockAgent }) {
  const question = agent.waiting_question;
  if (!question) return null;
  const label = question.state === "answered" ? "Answer queued"
    : question.state === "supervisor" ? "Awaiting supervisor" : "Waiting for your reply";
  return (
    <span className="block text-amber-300">
      <span className="block">{label}</span>
      <span className="block truncate text-gray-400" title={question.question}>{question.question}</span>
    </span>
  );
}

/** The flock's own total, for the rail header.
 *
 * Rendered from the daemon's rollup rather than summed in the browser: a
 * client-side sum has to guess what an unknown native count means, and the
 * two would drift the moment either side changed.
 */
export function FlockSubagents({ rollup }: { rollup: SubagentRollup | null | undefined }) {
  if (!rollup || rollup.active_total == null) return null;
  const complete = rollup.complete !== false;
  const total = rollup.active_total ?? 0;
  if (!total && complete) return null;
  return (
    <span
      className="shrink-0 font-mono text-[10px] text-gray-500"
      title={(complete ? "" : "At least ")
        + total + " active sub-agents across the flock: "
        + (rollup.native_total ?? 0) + " native, " + (rollup.aq_total ?? 0) + " AQ."
        + (complete ? "" : " Some live session was launched without harness hooks, "
          + "so its native sub-agents are unobserved.")}
    >
      {(complete ? "" : "≥") + total + " sub"}
    </span>
  );
}
