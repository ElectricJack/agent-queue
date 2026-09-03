import { useState } from "react";
import { usePlaybookActivationHealth, usePlaybookArtifactDiff, usePlaybookPendingEventAction, usePlaybookPendingEvents, usePlaybookRunOverlay, usePlaybookRuns, usePlaybookV2Graph, useSetPlaybookActivation } from "../../api/hooks";
import ActivationPanel from "./ActivationPanel";
import ArtifactDiffPanel from "./ArtifactDiffPanel";
import PendingEventsPanel from "./PendingEventsPanel";
import PlaybookSemanticGraphView from "./PlaybookSemanticGraphView";
import RunOverlayPanel from "./RunOverlayPanel";

export default function PlaybookSemanticReview({ playbookId }: { playbookId: string }) {
  const health = usePlaybookActivationHealth(playbookId);
  const activation = health.data?.activations?.[0];
  const [runId, setRunId] = useState<string>();
  const overlay = usePlaybookRunOverlay(runId);
  const artifactSha = overlay.data?.artifact.artifact_sha256 ?? activation?.active_artifact_sha256 ?? undefined;
  const graph = usePlaybookV2Graph(playbookId, { artifactSha });
  const diff = usePlaybookArtifactDiff(playbookId, artifactSha, activation?.active_artifact_sha256 ?? undefined);
  const pending = usePlaybookPendingEvents(playbookId);
  const runs = usePlaybookRuns(playbookId);
  const activate = useSetPlaybookActivation();
  const pendingAction = usePlaybookPendingEventAction();
  if (health.isPending) return <p className="text-sm text-gray-500">Loading semantic review…</p>;
  if (!activation) return null;
  return <div className="space-y-4"><PlaybookSemanticGraphView playbookId={playbookId} artifactSha={artifactSha} /><ArtifactDiffPanel diff={diff.data} /><ActivationPanel artifact={graph.data?.artifact ?? ({ artifact_sha256: artifactSha ?? "" } as never)} activation={activation as never} executableChange={Boolean(diff.data?.executable_change)} onActivate={(sha) => activate.mutate({ playbook_id: playbookId, artifact_sha256: sha, acknowledge_diff: diff.data?.executable_change ? sha : undefined })} /><PendingEventsPanel events={(pending.data?.events ?? []) as never} onAction={(action, ids) => pendingAction.mutate({ action, pending_event_ids: ids })} /><section className="rounded-lg border border-gray-800 bg-gray-900 p-4"><label className="block text-sm text-gray-300" htmlFor="v2-run">Run overlay</label><select id="v2-run" value={runId ?? ""} onChange={(event) => setRunId(event.target.value || undefined)} className="mt-2 rounded bg-gray-800 p-2 text-sm text-gray-200"><option value="">Select a run</option>{(runs.data ?? []).map((run) => <option key={run.run_id} value={run.run_id}>{run.run_id}</option>)}</select><div className="mt-3"><RunOverlayPanel overlay={overlay.data as never} /></div></section></div>;
}
