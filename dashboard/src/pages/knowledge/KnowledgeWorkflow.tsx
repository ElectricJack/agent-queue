import { useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { knowledgeCreateTask, knowledgeRetire, knowledgeRestore, knowledgePropose, linkCreate } from "../../api/client";
import Modal from "../../components/Modal";
import { KNOWLEDGE_ACTION_LABELS, type KnowledgeAction, type KnowledgeDetailView } from "./model";

const FIELD = "w-full rounded border border-gray-700 bg-gray-950 p-2 text-sm";
export default function KnowledgeWorkflow({ projectId, children }: {
  projectId: string;
  children: (onAction: (action: KnowledgeAction, detail: KnowledgeDetailView) => void) => ReactNode;
}) {
  const client = useQueryClient();
  const [operation, setOperation] = useState<{ action: KnowledgeAction; detail: KnowledgeDetailView; key: string } | null>(null);
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [reason, setReason] = useState("");
  const [target, setTarget] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState<{ taskId: string; linkId: string; route: string; status: string; gates: string[] } | null>(null);
  const open = (action: KnowledgeAction, detail: KnowledgeDetailView) => {
    if (!detail.allowedActions.includes(action)) return;
    setOperation({ action, detail, key: crypto.randomUUID() });
    setTitle(""); setText(""); setReason(""); setTarget(""); setError(""); setReceipt(null);
  };
  const close = () => { if (!pending) setOperation(null); };
  async function submit() {
    if (!operation || pending) return;
    const { action, detail, key } = operation;
    const base = { project_id: projectId, identity: `record:${detail.recordId}`, idempotency_key: key, if_revision: detail.current.revisionId };
    setPending(true); setError("");
    try {
      if (action === "create_task") {
        const { data } = await knowledgeCreateTask({ body: { project_id: projectId, identity: base.identity,
          revision_id: detail.viewed.revisionId, title, description: text, idempotency_key: key } });
        if (!data) throw new Error("No receipt");
        setReceipt({ taskId: data.task_id, linkId: data.link_id, route: data.route_source, status: data.status,
          gates: Array.isArray(data.gate_ids) ? data.gate_ids as string[] : [] });
      } else if (action === "retire") {
        await knowledgeRetire({ body: { ...base, reason } }); setOperation(null);
      } else if (action === "restore") {
        await knowledgeRestore({ body: { ...base, reason, revision_id: detail.viewed.revisionId } }); setOperation(null);
      } else if (action === "propose_correction") {
        await knowledgePropose({ body: { ...base, snapshot: { ...detail.proposalSnapshot, body: text, verification: "unverified",
            lifecycle: "active", retirement_reason: null, successor_record_id: null,
            last_verified_at: null, last_verified_by: null, change_reason: reason } } }); setOperation(null);
      } else if (action === "link") {
        await linkCreate({ body: { ...base, operations: [{ action: "add", target, link_type: "references" }] } });
        setOperation(null);
      }
      await client.invalidateQueries({ queryKey: ["knowledge"] });
      await client.invalidateQueries({ queryKey: ["records"] });
      await client.invalidateQueries({ queryKey: ["tasks"] });
    } catch { setError("The request could not be saved. Reload or compare the current revision before changing your request."); }
    finally { setPending(false); }
  }
  return <>
    {children(open)}
    <Modal open={operation !== null} onClose={close} title={operation ? KNOWLEDGE_ACTION_LABELS[operation.action] : "Knowledge"}>
      {receipt ? <div role="status" className="space-y-2 text-sm">
        <p>Task created: <Link to={`/tasks/${encodeURIComponent(receipt.taskId)}`} className="text-indigo-300">{receipt.taskId}</Link></p>
        <p>Routing: {receipt.route}. Status: {receipt.status}.</p>
        <p>{receipt.gates.length ? `Gates: ${receipt.gates.join(", ")}` : "Ordinary task routing and approval controls apply."}</p>
        <p>Informational motivated by link: {receipt.linkId}</p>
      </div> : <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <p className="text-xs text-gray-400">{operation?.detail.title} · revision {operation?.detail.viewed.sequence}</p>
        {operation?.action === "create_task" && <>
          <p className="text-sm text-gray-300">Choose the work to file. This creates a separate task and a pinned informational link.</p>
          <label className="block text-sm">Task title<input className={FIELD} value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={240} /></label>
          <label className="block text-sm">Task description<textarea className={FIELD} value={text} onChange={(e) => setText(e.target.value)} required /></label>
        </>}
        {operation?.action === "propose_correction" && <label className="block text-sm">Proposed body<textarea className={FIELD} value={text} onChange={(e) => setText(e.target.value)} required /></label>}
        {operation?.action === "link" && <label className="block text-sm">Target identity<input className={FIELD} placeholder="record:UUID or knowledge:kn-…" value={target} onChange={(e) => setTarget(e.target.value)} required /></label>}
        {operation?.action === "restore" && <p className="text-sm text-amber-200">Restore this content as a new active, unverified revision. Verification and authority reset.</p>}
        {operation && ["retire", "restore", "propose_correction"].includes(operation.action) &&
          <label className="block text-sm">Reason<textarea className={FIELD} value={reason} onChange={(e) => setReason(e.target.value)} required /></label>}
        {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
        <button disabled={pending} className="rounded bg-indigo-600 px-3 py-2 text-sm disabled:opacity-50">{pending ? "Saving…" : "Submit"}</button>
      </form>}
    </Modal>
  </>;
}
