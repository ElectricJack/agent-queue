import { useState } from "react";

type Artifact = { artifact_sha256: string };
type Activation = { active_artifact_sha256?: string | null; health: string; reasons?: { code: string; message: string }[] };
export default function ActivationPanel({ artifact, activation, executableChange, onActivate }: { artifact: Artifact; activation: Activation; executableChange: boolean; onActivate: (sha: string) => void }) {
  const [acknowledgedSha, setAcknowledgedSha] = useState<string | null>(null);
  const acknowledged = !executableChange || acknowledgedSha === artifact.artifact_sha256;
  return <section aria-label="Activation review" className="space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-4"><h2 className="font-medium text-gray-100">Activation</h2><p className="break-all font-mono text-xs text-gray-400">Active: {activation.active_artifact_sha256 ?? "none"}</p><p className="text-sm text-gray-300">Health: {activation.health.replace(/_/g, " ")}</p>{activation.reasons?.map((reason) => <p key={reason.code} className="text-xs text-amber-300">{reason.message}</p>)}{executableChange && <label className="flex gap-2 text-sm text-gray-300"><input type="checkbox" aria-label="I reviewed the executable diff" checked={acknowledged} onChange={(event) => setAcknowledgedSha(event.target.checked ? artifact.artifact_sha256 : null)} />I reviewed the executable diff</label>}<button type="button" disabled={!acknowledged} onClick={() => onActivate(artifact.artifact_sha256)} className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white disabled:cursor-not-allowed disabled:bg-gray-700">Activate displayed artifact</button></section>;
}
