import { useState } from "react";
import { usePlaybookActivationHealth, usePlaybookArtifactDiff, usePlaybookArtifacts, usePlaybookPendingEventAction, usePlaybookPendingEvents, usePlaybookRunOverlay, usePlaybookRuns, usePlaybookV2Graph, useSetPlaybookActivation } from "../../api/hooks";
import ActivationPanel from "./ActivationPanel";
import ArtifactDiffPanel from "./ArtifactDiffPanel";
import PendingEventsPanel from "./PendingEventsPanel";
import PlaybookSemanticGraphView from "./PlaybookSemanticGraphView";
import RunOverlayPanel from "./RunOverlayPanel";

/** Lifecycles a run can still move out of. Anything else — including a status
 *  this build does not know — is treated as history and fetched once. */
const LIVE_LIFECYCLES = new Set(["running", "paused", "cancelling"]);

export default function PlaybookSemanticReview({ playbookId }: { playbookId: string }) {
  const health = usePlaybookActivationHealth(playbookId);
  const activation = health.data?.activations?.[0];
  const [runId, setRunId] = useState<string>();
  const [selectedSha, setSelectedSha] = useState<string>();
  const runs = usePlaybookRuns(playbookId);
  const artifacts = usePlaybookArtifacts(playbookId);
  // A run still in flight keeps its overlay polling, which is what makes the
  // graph's run state live; a finished run is history and is fetched once.
  const selectedRun = (runs.data ?? []).find((run) => run.run_id === runId);
  const overlay = usePlaybookRunOverlay(runId, {
    live: Boolean(selectedRun && LIVE_LIFECYCLES.has(selectedRun.status)),
  });
  const activeSha = activation?.active_artifact_sha256 ?? undefined;
  // Precedence: a selected run pins the artifact it actually executed, then an
  // explicitly chosen candidate, then the active one. Without the middle term
  // the only reachable artifact is the active one and the diff below is that
  // artifact against itself, which is no review at all.
  const artifactSha = overlay.data?.artifact.artifact_sha256 ?? selectedSha ?? activeSha;
  const graph = usePlaybookV2Graph(playbookId, { artifactSha });
  const diff = usePlaybookArtifactDiff(playbookId, artifactSha, activeSha);
  const pending = usePlaybookPendingEvents(playbookId);
  const activate = useSetPlaybookActivation();
  const pendingAction = usePlaybookPendingEventAction();
  const candidates = artifacts.data?.artifacts ?? [];
  const selectedEntry = candidates.find((entry) => entry.artifact.artifact_sha256 === artifactSha);
  if (health.isPending) return <p className="text-sm text-gray-500">Loading semantic review…</p>;
  if (!activation) return null;
  return <div className="space-y-4"><PlaybookSemanticGraphView playbookId={playbookId} artifactSha={artifactSha} overlay={overlay.data} /><ArtifactDiffPanel diff={diff.data} /><ActivationPanel artifact={graph.data?.artifact ?? selectedEntry?.artifact ?? ({ artifact_sha256: artifactSha ?? "" } as never)} activation={activation as never} executableChange={Boolean(diff.data?.executable_change)} activationBlocked={Boolean(diff.data?.activation_blocked)} activationBlockers={diff.data?.activation_blockers ?? []} artifacts={candidates} selectedSha={artifactSha} onSelect={(sha) => { setSelectedSha(sha); setRunId(undefined); }} onActivate={(sha) => activate.mutate({ playbook_id: playbookId, artifact_sha256: sha, acknowledge_diff: diff.data?.executable_change ? sha : undefined })} /><PendingEventsPanel events={(pending.data?.events ?? []) as never} onAction={(action, ids) => pendingAction.mutate({ action, pending_event_ids: ids })} /><section className="rounded-lg border border-gray-800 bg-gray-900 p-4"><label className="block text-sm text-gray-300" htmlFor="v2-run">Run overlay</label><select id="v2-run" value={runId ?? ""} onChange={(event) => setRunId(event.target.value || undefined)} className="mt-2 rounded bg-gray-800 p-2 text-sm text-gray-200"><option value="">Select a run</option>{(runs.data ?? []).map((run) => <option key={run.run_id} value={run.run_id}>{run.run_id}</option>)}</select><div className="mt-3"><RunOverlayPanel overlay={overlay.data as never} /></div></section></div>;
}
