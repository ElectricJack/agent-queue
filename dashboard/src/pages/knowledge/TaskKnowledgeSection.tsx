import { useMemo, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useKnowledgeCapabilities } from "../../api/knowledge";
import { knowledgeCreate, recordShow } from "../../api/client";
import TaskKnowledgePanel from "../../panes/knowledge/TaskKnowledgePanel";
import Modal from "../../components/Modal";
import { createLiveKnowledgeAdapter } from "./liveAdapter";

export default function TaskKnowledgeSection({ projectId, taskId, selectedText, children }: {
  projectId: string; taskId: string; selectedText?: string; children?: ReactNode;
}) {
  const selectionRoot = useRef<HTMLDivElement>(null);
  const [selectionText, setSelectionText] = useState("");
  const observedToken = useRef<string | null>(null);
  const captureSelection = () => {
    const selection = window.getSelection();
    setSelectionText(selection?.rangeCount && selectionRoot.current?.contains(selection.getRangeAt(0).commonAncestorContainer) ? selection.toString() : "");
  };
  const description = <div ref={selectionRoot} onMouseUp={captureSelection} onKeyUp={captureSelection}>{children}</div>;
  const caps = useKnowledgeCapabilities(projectId);
  const adapter = useMemo(() => createLiveKnowledgeAdapter(projectId), [projectId]);
  const navigate = useNavigate();
  const client = useQueryClient();
  const [finding, setFinding] = useState<{ text: string; key: string } | null>(null);
  const [title, setTitle] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  if (!caps.data?.available) return description;
  const canSave = caps.data.writes && caps.data.operations.includes("knowledge_create") && caps.data.operations.includes("link_create");
  async function save() {
    if (!finding || pending) return;
    setPending(true); setError("");
    try {
      if (observedToken.current === null) {
        const { data: task } = await recordShow({ body: { project_id: projectId, identity: `task:${taskId}` } });
        if (typeof task?.link_token !== "string") throw new Error("No link guard");
        observedToken.current = task.link_token;
      }
      const { data } = await knowledgeCreate({ body: { project_id: projectId, title, body: finding.text, category: "note",
        idempotency_key: finding.key, source_task_id: taskId, if_link_token: observedToken.current,
        sources: [{ source_id: `task:${taskId}`, kind: "task", task_id: taskId }] } });
      if (!data?.record_id) throw new Error("No receipt");
      setFinding(null);
      await client.invalidateQueries({ queryKey: ["knowledge"] });
      navigate(`/projects/${encodeURIComponent(projectId)}/knowledge?record=${encodeURIComponent(data.record_id)}`);
    } catch { setError("The finding could not be saved. Your selected text is retained."); }
    finally { setPending(false); }
  }
  return <>
    {description}
    <TaskKnowledgePanel taskId={taskId} adapter={adapter} selectedText={selectedText ?? selectionText}
      onOpenRecord={(id, revision) => navigate(`/projects/${encodeURIComponent(projectId)}/knowledge?record=${encodeURIComponent(id)}${revision ? `&revision=${encodeURIComponent(revision)}` : ""}`)}
      onSaveFinding={canSave ? ({ text }) => { observedToken.current = null; setFinding({ text, key: crypto.randomUUID() }); setTitle(""); setError(""); } : undefined} />
    <Modal open={finding !== null} onClose={() => { if (!pending) setFinding(null); }} title="Save finding">
      <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <label className="block text-sm">Finding title<input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={240} className="w-full rounded border border-gray-700 bg-gray-950 p-2" /></label>
        <p className="whitespace-pre-wrap text-sm">{finding?.text}</p>
        <p className="text-xs text-gray-400">Save only this selected text as an unverified finding with a pinned produces link from the task.</p>
        {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
        <button disabled={pending} className="rounded bg-indigo-600 px-3 py-2 text-sm">{pending ? "Saving…" : "Save finding"}</button>
      </form>
    </Modal>
  </>;
}
