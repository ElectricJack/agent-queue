import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useKnowledgeCapabilities } from "../../api/knowledge";
import { createLiveKnowledgeAdapter } from "../../pages/knowledge/liveAdapter";
import KnowledgeWorkflow from "../../pages/knowledge/KnowledgeWorkflow";
import type { PaneViewProps } from "../types";
import type { KnowledgeArgs } from "./manifest";
import KnowledgePane from "./KnowledgePane";

export default function LiveKnowledgePane({ args }: PaneViewProps<KnowledgeArgs>) {
  return <ScopedKnowledgePane key={`${args.projectId}:${args.recordId}:${args.revisionId ?? ""}`} args={args} />;
}
function ScopedKnowledgePane({ args }: { args: KnowledgeArgs }) {
  const caps = useKnowledgeCapabilities(args.projectId);
  const adapter = useMemo(() => createLiveKnowledgeAdapter(args.projectId), [args.projectId]);
  const [selection, setSelection] = useState({ recordId: args.recordId, revisionId: args.revisionId ?? null });
  const navigate = useNavigate();
  if (!caps.data?.available) return <p role="status">Knowledge is unavailable for this project.</p>;
  return <KnowledgeWorkflow projectId={args.projectId}>{(onAction) =>
    <KnowledgePane adapter={adapter} {...selection}
      onRevisionChange={(revisionId) => setSelection((current) => ({ ...current, revisionId }))}
      onOpenRecord={(recordId, revisionId) => setSelection({ recordId, revisionId })}
      onOpenTask={(taskId) => navigate(`/tasks/${encodeURIComponent(taskId)}`)} onAction={onAction} />
  }</KnowledgeWorkflow>;
}
