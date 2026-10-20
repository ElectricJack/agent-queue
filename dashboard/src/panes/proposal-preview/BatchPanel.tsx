import type { IngestBatchDetail } from "../../api/client";
import { useShellPaneStore } from "../store";
import { useTask } from "../../api/hooks";

export function Evidence({ label, value }: { label: string; value: unknown }) {
  return <details className="rounded border border-gray-800 p-2">
    <summary className="cursor-pointer text-xs font-medium">{label}</summary>
    <pre className="mt-2 whitespace-pre-wrap break-words text-xs text-gray-300">
      {value == null ? "None recorded" : JSON.stringify(value, null, 2)}
    </pre>
  </details>;
}

function OperationalRoot({ taskId }: { taskId: string }) {
  const task = useTask(taskId);
  const pane = useShellPaneStore();
  if (task.isError) return <p role="alert">Could not load delivery condition and owner for {taskId}.</p>;
  if (!task.data) return <p role="status">Loading delivery condition and owner…</p>;
  const targets = task.data.depends_on?.filter((dep) => dep.dep_type === "delivery-of") ?? [];
  return <div className="mt-1 space-y-1">
    <p>Owner: {task.data.assigned_agent ?? "Unassigned"}</p>
    <Evidence label="Recorded delivery condition" value={task.data.delivery_condition} />
    <p>Delivery condition: {targets.length ? "Requires landed delivery of:" : "No delivery prerequisites recorded."}</p>
    {targets.map((target) => <button key={target.id} type="button" className="mr-2 text-indigo-300 hover:underline"
      onClick={() => pane.open("task-detail", { taskId: target.id })}>{target.title} ({target.id})</button>)}
  </div>;
}

/** Evidence is revision-bound by the API. Historical receipts never stand in
 * for missing current verdicts, and a passing review is not a release. */
export default function BatchPanel({ batch }: { batch: IngestBatchDetail }) {
  const pane = useShellPaneStore();
  const sections = Object.entries(batch.section_verdicts ?? {});
  return <section aria-label="Fidelity review batch" className="space-y-3 text-xs">
    <h3 className="font-semibold text-sm">
      {batch.state === "released" ? "Batch released" : batch.state === "retired" ? "Batch retired" : "Held for fidelity review"}
    </h3>
    <p>State: {batch.state} · Structure revision {batch.structure_revision}</p>
    <p className="break-all">Structure digest: {batch.structure_digest}</p>
    <p className="break-words">Source: {batch.source} · Review {batch.source_review_id} revision {batch.source_revision}</p>
    <p className="break-all">Source hash: {batch.source_content_sha256}</p>
    <div>
      <p className="font-medium">Design lineage{batch.lineage.lineage_kind === "none" ? ": none" : ""}</p>
      <Evidence label="Lineage evidence" value={batch.lineage} />
    </div>
    <p>Reviewer: {batch.reviewer_task_id ? <button type="button" className="text-indigo-300 hover:underline"
      onClick={() => pane.open("task-detail", { taskId: batch.reviewer_task_id! })}>{batch.reviewer_task_id}</button> : "Not assigned"}</p>
    {batch.state === "needs_scope" && <p role="status" className="text-amber-300">A scope question needs an answer. The batch remains held.</p>}
    <section aria-label="Per-section fidelity verdicts" className="space-y-2">
      <h4 className="font-medium">Per-section fidelity verdicts · revision {batch.structure_revision}</h4>
      {sections.length ? sections.map(([section, verdict]) => <div key={section}>
        <p className="font-medium">{section}</p>
        <p className="whitespace-pre-wrap break-words">{typeof verdict === "string" ? verdict : JSON.stringify(verdict)}</p>
      </div>) : <p>No current verdicts recorded.</p>}
      <p>Reverse coverage: {batch.reverse_coverage_verdict ?? "Not recorded"}</p>
      <p>End-to-end walkthrough: {batch.walkthrough_verdict ?? "Not recorded"}</p>
    </section>
    <Evidence label="Automatic checks" value={batch.check_results} />
    <Evidence label="Structural findings and dispositions" value={batch.structural_findings ?? []} />
    <Evidence label="Current diff" value={batch.diff} />
    <section aria-label="Batch membership" className="space-y-2">
      <h4 className="font-medium">Materialized tasks ({Object.keys(batch.members).length} active)</h4>
      {batch.membership.map((member, index) => <div key={String(member.task_id ?? index)} className="rounded border border-gray-800 p-2">
        <button type="button" className="text-indigo-300 hover:underline" onClick={() => pane.open("task-detail", { taskId: String(member.task_id) })}>
          {String(member.task_id)}
        </button>
        <p>{String(member.role)} · {String(member.state)} · {String(member.section_anchor ?? "")}</p>
        <p className="whitespace-pre-wrap">{String(member.primary_outcome ?? "")}</p>
        {member.role === "operational_root" && <OperationalRoot taskId={String(member.task_id)} />}
      </div>)}
    </section>
    <Evidence label="Review receipt history (may include superseded revisions)" value={batch.receipts ?? []} />
    <section aria-label="Release receipt" className="space-y-1">
      <h4 className="font-medium">Release receipt</h4>
      {batch.release_receipt ? <>
        <p>Actor: {batch.release_actor ?? "Not recorded"} · Revision {batch.release_revision ?? "Not recorded"}</p>
        <p className="break-all">Digest: {batch.release_digest ?? "Not recorded"}</p>
        <p>Released: {batch.released_at == null ? "Not recorded" : new Date(batch.released_at * 1000).toLocaleString()}</p>
        <Evidence label="Release proof" value={batch.release_receipt} />
      </> : <p>No release receipt recorded.</p>}
    </section>
  </section>;
}
