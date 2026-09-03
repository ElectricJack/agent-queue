type FieldChange = { path: string; executable?: boolean };
type Step = { step_id: string; change: string; field_changes?: FieldChange[] };
type Diff = { base?: unknown; target?: unknown; executable_change?: boolean; semantic_change_count?: number; presentation_change_count?: number; activation_blockers?: string[]; steps?: Step[] };

export default function ArtifactDiffPanel({ diff }: { diff?: Diff }) {
  if (!diff) return <p className="text-sm text-gray-500">Select an artifact to review its semantic diff.</p>;
  const executable = diff.steps?.flatMap((step) => step.field_changes?.filter((field) => field.executable !== false).map((field) => `${step.step_id}${field.path}`) ?? []) ?? [];
  const presentation = diff.steps?.flatMap((step) => step.field_changes?.filter((field) => field.executable === false).map((field) => `${step.step_id}${field.path}`) ?? []) ?? [];
  return <section aria-label="Artifact diff" className="space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-4"><h2 className="font-medium text-gray-100">Artifact review</h2><p className="text-sm text-gray-400">{diff.semantic_change_count ?? 0} semantic and {diff.presentation_change_count ?? 0} presentation changes.</p><DiffList title="Executable changes" changes={executable} /><DiffList title="Presentation-only changes" changes={presentation} />{diff.activation_blockers?.map((blocker) => <p key={blocker} className="text-sm text-amber-300">{blocker}</p>)}</section>;
}
function DiffList({ title, changes }: { title: string; changes: string[] }) { return <div><h3 className="text-sm font-medium text-gray-200">{title}</h3>{changes.length ? <ul className="ml-5 list-disc text-xs text-gray-400">{changes.map((change) => <li key={change}>{change}</li>)}</ul> : <p className="text-xs text-gray-500">None</p>}</div>; }
