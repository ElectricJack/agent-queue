import { useState } from "react";

type Artifact = { artifact_sha256: string; version?: number };
type ArtifactSummary = { artifact: Artifact; is_active?: boolean; created_at?: number | null };
type Activation = { active_artifact_sha256?: string | null; health: string; reasons?: { code: string; message: string }[] };

/** A hash is unreadable in full and ambiguous truncated to four; twelve hex
 *  digits is what the rest of the V2 surface shows. */
function shortSha(sha: string) {
  return sha.replace(/^sha256:/, "").slice(0, 12);
}

function optionLabel(entry: ArtifactSummary) {
  const version = entry.artifact.version ?? 0;
  const name = `v${version} ${shortSha(entry.artifact.artifact_sha256)}`;
  return entry.is_active ? `${name} (active)` : name;
}

export default function ActivationPanel({ artifact, activation, executableChange, activationBlocked = false, activationBlockers = [], onActivate, artifacts = [], selectedSha, onSelect }: { artifact: Artifact; activation: Activation; executableChange: boolean; activationBlocked?: boolean; activationBlockers?: string[]; onActivate: (sha: string) => void; artifacts?: ArtifactSummary[]; selectedSha?: string; onSelect?: (sha: string) => void }) {
  const [acknowledgedSha, setAcknowledgedSha] = useState<string | null>(null);
  const acknowledged = !executableChange || acknowledgedSha === artifact.artifact_sha256;
  // The server refuses an activation whose diff reports a blocker (an unknown
  // command or a stale contract), so offering the button would be an
  // affordance that cannot succeed.  A blockers list without the flag still
  // blocks: either half of the pair is enough evidence.
  const blockers = activationBlockers ?? [];
  const blocked = activationBlocked || blockers.length > 0;
  // The chooser is what makes an *inactive* candidate reviewable: without it
  // the only reachable artifact is the active one, and the diff below is the
  // active artifact against itself.
  const chooser = artifacts.length > 0 && onSelect;
  return <section aria-label="Activation review" className="space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-4"><h2 className="font-medium text-gray-100">Activation</h2>{chooser && <div><label className="block text-sm text-gray-300" htmlFor="v2-artifact">Artifact under review</label><select id="v2-artifact" value={selectedSha ?? artifact.artifact_sha256} onChange={(event) => onSelect(event.target.value)} className="mt-1 rounded bg-gray-800 p-2 font-mono text-xs text-gray-200">{artifacts.map((entry) => <option key={entry.artifact.artifact_sha256} value={entry.artifact.artifact_sha256}>{optionLabel(entry)}</option>)}</select></div>}<p className="break-all font-mono text-xs text-gray-400">Active: {activation.active_artifact_sha256 ?? "none"}</p><p className="break-all font-mono text-xs text-gray-400">Reviewing: {artifact.artifact_sha256 || "none"}</p><p className="text-sm text-gray-300">Health: {activation.health.replace(/_/g, " ")}</p>{activation.reasons?.map((reason) => <p key={reason.code} className="text-xs text-amber-300">{reason.message}</p>)}{executableChange && <label className="flex gap-2 text-sm text-gray-300"><input type="checkbox" aria-label="I reviewed the executable diff" checked={acknowledged} onChange={(event) => setAcknowledgedSha(event.target.checked ? artifact.artifact_sha256 : null)} />I reviewed the executable diff</label>}{blocked && <div role="alert" className="space-y-1"><p className="text-sm text-amber-300">Activation is blocked:</p>{blockers.map((blocker) => <p key={blocker} className="text-xs text-amber-300">{blocker}</p>)}</div>}<button type="button" disabled={!acknowledged || blocked} onClick={() => onActivate(artifact.artifact_sha256)} className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white disabled:cursor-not-allowed disabled:bg-gray-700">Activate displayed artifact</button></section>;
}
