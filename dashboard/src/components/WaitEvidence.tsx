import type { WaitExplanation } from "../api/waits";
import StatusBadge from "./StatusBadge";

function waitAge(wait: WaitExplanation): string {
  if (wait.age_unknown || wait.age_seconds == null) return "age_unknown";
  const seconds = Math.floor(wait.age_seconds);
  return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}

export function VerifiedWaitEvidence({ wait }: { wait: WaitExplanation }) {
  const evidence = wait.verified;
  return <div className="space-y-1 break-words text-xs text-gray-300">
    <div className="flex flex-wrap items-center gap-2">
      <strong>{evidence?.kind ?? "Unknown source"}</strong>
      <StatusBadge status={evidence?.state ?? "unavailable"} />
    </div>
    <p>Condition: <code>{JSON.stringify(evidence?.condition ?? {})}</code></p>
    <p>Evidence: <code>{evidence?.evidence ?? "Unavailable"}</code></p>
    <p>Source: <code>{evidence?.source_key ?? "Unknown"}</code> · generation {evidence?.source_generation ?? "unknown"} · revision {evidence?.source_revision ?? "unknown"}</p>
    <p>Observed: {evidence?.observed_at == null ? "Unknown" : new Date(evidence.observed_at * 1000).toLocaleString()} · freshness {evidence?.freshness_seconds == null ? "unknown" : `${Math.floor(evidence.freshness_seconds)}s`}</p>
    {Object.keys(evidence?.facts ?? {}).length > 0 && <p>Facts: <code>{JSON.stringify(evidence?.facts)}</code></p>}
  </div>;
}

export function WaitDetails({ wait }: { wait: WaitExplanation }) {
  const priority = wait.effective_priority;
  return <div className="space-y-1 break-words text-xs text-gray-400">
    <p>Age: {waitAge(wait)} · Outcome: {wait.outcome ?? "pending"}</p>
    <p>Priority: base {priority?.base ?? "unknown"}, effective {priority?.value ?? "unknown"}
      {priority?.donor && <> · donor {priority.donor} via {priority.donor_path?.join(" → ")}</>}</p>
    <p>Sleep: {wait.sleep?.decision ?? "undecided"}{wait.sleep?.reason && ` · ${wait.sleep.reason}`}</p>
    <p>Policy layer: {wait.policy_layer ?? "default"}</p>
    {(wait.deadlines ?? []).map((deadline, i) => <p key={`${deadline.type}-${i}`}>
      {deadline.type}: {new Date(deadline.due_at * 1000).toLocaleString()}
      {deadline.cancelled_at != null ? " (cancelled)" : deadline.fired_at != null ? " (fired)" : ""}
    </p>)}
    {(wait.chain ?? []).map((edge, i) => <p key={edge.id ?? i}>{edge.from} → {edge.to} ({edge.relation})</p>)}
    {wait.chain_truncated && <p role="status">Blocker chain truncated.</p>}
    <details><summary className="cursor-pointer">Decision inputs, reservations and continuation</summary>
      <pre className="mt-1 whitespace-pre-wrap">{JSON.stringify({
        inputs: wait.sleep?.inputs, reservations: wait.reservations,
        continuation: wait.continuation, escalation: wait.escalation,
      }, null, 2)}</pre>
    </details>
  </div>;
}
