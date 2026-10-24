import type { Task } from "../../api/hooks";
import { useShellPaneStore } from "../store";
import { isIngestHeld, statusPillFor } from "../../pages/command-center/statusPillModel";
import { StatusPill } from "../../pages/command-center/StatusPill";
import { Evidence } from "../proposal-preview/BatchPanel";

/** The header pill for a task held in an ingestion batch. */
export function HeldStatusPill({ task }: { task: Task }) {
  return <StatusPill model={statusPillFor({ status: task.status ?? "", held_batch: task.held_batch })} />;
}

/** The held ingestion batch and the operational delivery condition sections of task detail. */
export default function IngestionSections({ task, onOpenTask }: { task: Task; onOpenTask: (taskId: string) => void }) {
  const pane = useShellPaneStore();
  return (
    <>
      {task.held_batch && <section aria-label="Ingestion batch" className="space-y-2 rounded border border-gray-800 p-3 text-xs text-gray-300">
        <h3 className="font-semibold">{isIngestHeld(task.held_batch) ? "Held for fidelity review" : `Ingestion batch · ${task.held_batch.state}`}</h3>
        <p className="break-words">Source: {task.held_batch.source} · Structure revision {task.held_batch.structure_revision}</p>
        <p>Reviewer: {task.held_batch.reviewer_task_id ? <button type="button" className="text-indigo-300 hover:underline"
          onClick={() => onOpenTask(task.held_batch!.reviewer_task_id!)}>{task.held_batch.reviewer_task_id}</button> : "Not assigned"}</p>
        <Evidence label={`Design lineage${task.held_batch.lineage.lineage_kind === "none" ? ": none" : ""}`} value={task.held_batch.lineage} />
        <button type="button" className="text-indigo-300 hover:underline" onClick={() => pane.open("proposal-preview", {
          proposalId: task.held_batch!.batch_id, batchProjectId: task.project_id,
        })}>View batch review and release evidence</button>
      </section>}

      {(task.delivery_condition || task.depends_on?.some((dep) => dep.dep_type === "delivery-of")) && <section aria-label="Operational delivery condition" className="space-y-2 rounded border border-gray-800 p-3 text-xs text-gray-300">
        <h3 className="font-semibold">Delivery condition</h3>
        <p>Starts after these sources have landed. Completion alone does not satisfy delivery.</p>
        <Evidence label="Recorded delivery condition" value={task.delivery_condition} />
        {task.depends_on?.filter((dep) => dep.dep_type === "delivery-of").map((dep) => <div key={dep.id}>
          <button type="button" className="text-indigo-300 hover:underline" onClick={() => onOpenTask(dep.id)}>{dep.title} ({dep.id})</button>
          {dep.reason && <p>{dep.reason}</p>}
        </div>)}
        <p>Owner: {task.assigned_agent ?? "Unassigned"}</p>
      </section>}
    </>
  );
}
