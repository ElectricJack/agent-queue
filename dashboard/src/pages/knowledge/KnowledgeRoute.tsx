import { useMemo } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useKnowledgeCapabilities } from "../../api/knowledge";
import Knowledge from "./Knowledge";
import KnowledgeWorkflow from "./KnowledgeWorkflow";
import { createLiveKnowledgeAdapter } from "./liveAdapter";
import { readKnowledgeFilters, readKnowledgeSelection, writeKnowledgeFilters, writeKnowledgeSelection } from "./knowledgeUrlState";

export default function KnowledgeRoute() {
  const { projectId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const capabilities = useKnowledgeCapabilities(projectId);
  const adapter = useMemo(() => createLiveKnowledgeAdapter(projectId), [projectId]);
  if (capabilities.isPending) return <p role="status">Loading knowledge…</p>;
  if (!capabilities.data?.available) return <p role="status">Knowledge is unavailable for this project.</p>;
  return <KnowledgeWorkflow projectId={projectId}>{(onAction) =>
    <Knowledge key={projectId} adapter={adapter} state={{ filters: readKnowledgeFilters(params), selection: readKnowledgeSelection(params) }} onOpenTask={(id) => navigate(`/tasks/${encodeURIComponent(id)}`)}
      onAction={onAction} onStateChange={({ filters, selection }) =>
        setParams((previous) => writeKnowledgeSelection(writeKnowledgeFilters(previous, filters), selection), {
          replace: selection.recordId === readKnowledgeSelection(params).recordId
            && selection.revisionId === readKnowledgeSelection(params).revisionId,
        })} />
  }</KnowledgeWorkflow>;
}
