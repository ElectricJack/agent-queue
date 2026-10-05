import { useMemo } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useKnowledgeCapabilities } from "../../api/knowledge";
import KnowledgePane from "../../panes/knowledge/KnowledgePane";
import KnowledgeWorkflow from "../knowledge/KnowledgeWorkflow";
import { createLiveKnowledgeAdapter } from "../knowledge/liveAdapter";
import { knowledgeSelectionHref, taskSelectionHref } from "./recordUrlState";

/** An explicit full-page destination; retired knowledge tab URLs redirect to the list. */
export default function KnowledgeFullPage() {
  const { projectId = "", recordId = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const caps = useKnowledgeCapabilities(projectId);
  const adapter = useMemo(() => createLiveKnowledgeAdapter(projectId), [projectId]);
  if (caps.isPending) return <p role="status">Loading knowledge…</p>;
  if (!caps.data?.available) return <p role="status">Knowledge is unavailable for this project.</p>;
  const from = (location.state as { from?: string } | null)?.from ?? knowledgeSelectionHref(projectId, recordId, params.get("revision"));
  return <div className="flex h-full min-h-0 flex-col">
    <Link to={from} className="shrink-0 p-3 text-sm text-indigo-300">Back to Tasks & Knowledge</Link>
    <div className="min-h-0 flex-1">
    <KnowledgeWorkflow projectId={projectId}>{(onAction) =>
      <KnowledgePane key={recordId} adapter={adapter} recordId={recordId} revisionId={params.get("revision")}
        onRevisionChange={(revision) => setParams((previous) => {
          const next = new URLSearchParams(previous);
          if (revision) next.set("revision", revision); else next.delete("revision");
          return next;
        })}
        onOpenRecord={(id, revision) => navigate(knowledgeSelectionHref(projectId, id, revision))}
        onOpenTask={(id) => navigate(taskSelectionHref(projectId, id))} onAction={onAction} />
    }</KnowledgeWorkflow>
    </div>
  </div>;
}
