import type {
  ContractChangeDTO,
  EdgeDiffDTO,
  ExplanationValueDTO,
  FieldChangeDTO,
  PlaybookArtifactDiffResponse,
  RuleDiffDTO,
  StepDiffDTO,
} from "../../api/client";
import DiagnosticsBanner from "./DiagnosticsBanner";

type DiffChange = ContractChangeDTO["change"];

/** A rendered value plus, when it is truncated, the full text for the title. */
type DiffValue = { text: string; title?: string };

/** One reviewable line. `identity` names the thing that changed, so an
 *  acknowledgement can be traced back to it, and `before`/`after` are what it
 *  changed from and to. Every semantic collection of the diff — steps, rules,
 *  transitions, contracts — projects into this shape; an operator who has to
 *  acknowledge a change must be able to see it. */
type DiffRow = {
  key: string;
  kind: string;
  identity: string;
  change: DiffChange;
  before?: DiffValue | null;
  after?: DiffValue | null;
  note?: string;
};

const CHANGE_TONES: Record<string, string> = {
  added: "bg-emerald-950 text-emerald-200",
  removed: "bg-red-950 text-red-200",
  modified: "bg-amber-950 text-amber-200",
  unchanged: "bg-gray-800 text-gray-400",
};

function literal(text?: string | null): DiffValue | null {
  return text ? { text } : null;
}

/** Contract fingerprints are full digests; twelve hex digits is what the rest
 *  of the V2 surface shows, and the full value stays reachable as a tooltip. */
function fingerprint(value?: string | null): DiffValue | null {
  if (!value) return null;
  const bare = value.replace(/^sha256:/, "");
  return bare.length > 12 ? { text: `${bare.slice(0, 12)}…`, title: value } : { text: bare };
}

function valueText(value?: ExplanationValueDTO | null): string | null {
  return value ? value.display : null;
}

function fieldChange(field: FieldChangeDTO): DiffChange {
  if (!field.before && field.after) return "added";
  if (field.before && !field.after) return "removed";
  return "modified";
}

function stepRows(steps: StepDiffDTO[], executable: boolean): DiffRow[] {
  return steps.flatMap((step) =>
    (step.field_changes ?? [])
      .filter((field) => (field.executable !== false) === executable)
      .map((field) => ({
        key: `step:${step.step_id}${field.path}`,
        kind: "step",
        identity: `${step.step_id}${field.path}`,
        change: fieldChange(field),
        before: literal(valueText(field.before)),
        after: literal(valueText(field.after)),
      })),
  );
}

function ruleRows(rules: RuleDiffDTO[]): DiffRow[] {
  return rules
    .filter((rule) => rule.change !== "unchanged")
    .map((rule) => {
      const added = rule.step_ids_added ?? [];
      const removed = rule.step_ids_removed ?? [];
      const notes = [];
      if (added.length) notes.push(`steps added: ${added.join(", ")}`);
      if (removed.length) notes.push(`steps removed: ${removed.join(", ")}`);
      return {
        key: `rule:${rule.rule_id}`,
        kind: "rule",
        identity: rule.rule_id,
        change: rule.change,
        before: literal(rule.event_type_before),
        after: literal(rule.event_type_after),
        note: notes.join("; ") || undefined,
      };
    });
}

function edgeRows(edges: EdgeDiffDTO[]): DiffRow[] {
  return edges
    .filter((edge) => edge.change !== "unchanged")
    .map((edge) => {
      // The DTO carries a single source/target/outcome triple. Its id pins the
      // rule, source and outcome, so a removed transition's triple is the one
      // the base artifact had and every other change's is the target's.
      const shape = literal(`${edge.source} → ${edge.target} on ${edge.outcome}`);
      return {
        key: `edge:${edge.edge_id}`,
        kind: "transition",
        identity: edge.edge_id,
        change: edge.change,
        before: edge.change === "removed" ? shape : null,
        after: edge.change === "removed" ? null : shape,
      };
    });
}

function contractRows(contracts: ContractChangeDTO[]): DiffRow[] {
  return contracts
    .filter((contract) => contract.change !== "unchanged")
    .map((contract) => ({
      key: `contract:${contract.command}`,
      kind: "contract",
      identity: contract.command,
      change: contract.change,
      before: fingerprint(contract.fingerprint_before),
      after: fingerprint(contract.fingerprint_after),
    }));
}

export default function ArtifactDiffPanel({ diff }: { diff?: PlaybookArtifactDiffResponse }) {
  if (!diff)
    return <p className="text-sm text-gray-500">Select an artifact to review its semantic diff.</p>;
  const steps = diff.steps ?? [];
  // A contract-only or structural change increments `semantic_change_count`
  // and demands an acknowledgement without touching a single step field, so
  // every semantic collection belongs in this list, not just the steps.
  const executable = [
    ...stepRows(steps, true),
    ...ruleRows(diff.rules ?? []),
    ...edgeRows(diff.edges ?? []),
    ...contractRows(diff.contracts ?? []),
  ];
  const presentation = stepRows(steps, false);
  const diagnostics = diff.diagnostics ?? [];
  // `activation_blockers` is derived from the diagnostics, so anything the
  // banner already shows would otherwise be printed twice.
  const shown = new Set(diagnostics.map((diagnostic) => diagnostic.message));
  const blockers = (diff.activation_blockers ?? []).filter((blocker) => !shown.has(blocker));
  // The count is the server's and the rows are ours: never show "None" against
  // a non-zero count, or the operator acknowledges something invisible.
  const unitemized = (diff.semantic_change_count ?? 0) > 0 && executable.length === 0;
  return (
    <section
      aria-label="Artifact diff"
      className="space-y-3 rounded-lg border border-gray-800 bg-gray-900 p-4"
    >
      <h2 className="font-medium text-gray-100">Artifact review</h2>
      <p className="text-sm text-gray-400">
        {diff.semantic_change_count ?? 0} semantic and {diff.presentation_change_count ?? 0}{" "}
        presentation changes.
      </p>
      <DiffList title="Executable changes" rows={executable} />
      {unitemized && (
        <p className="text-xs text-amber-300">
          The server counted semantic changes this artifact diff does not itemize. Review the
          artifact source before activating.
        </p>
      )}
      <DiffList title="Presentation-only changes" rows={presentation} />
      {diagnostics.length > 0 && (
        <div className="space-y-1">
          <h3 className="text-sm font-medium text-gray-200">Diagnostics</h3>
          <DiagnosticsBanner diagnostics={diagnostics} />
        </div>
      )}
      {blockers.map((blocker) => (
        <p key={blocker} className="text-sm text-amber-300">
          {blocker}
        </p>
      ))}
    </section>
  );
}

function DiffList({ title, rows }: { title: string; rows: DiffRow[] }) {
  return (
    <div>
      <div className="flex items-baseline gap-2">
        <h3 className="text-sm font-medium text-gray-200">{title}</h3>
        {rows.length > 0 && <span className="text-xs text-gray-500">{rows.length}</span>}
      </div>
      {rows.length ? (
        <ul aria-label={title} className="ml-5 list-disc space-y-0.5 text-xs text-gray-400">
          {rows.map((row) => (
            <DiffRowItem key={row.key} row={row} />
          ))}
        </ul>
      ) : (
        <p className="text-xs text-gray-500">None</p>
      )}
    </div>
  );
}

function DiffRowItem({ row }: { row: DiffRow }) {
  // A value that did not move is stated once: "a → a" reads as a change.
  const same = Boolean(row.before && row.after && row.before.text === row.after.text);
  return (
    <li className="flex flex-wrap items-baseline gap-x-2">
      <span className="shrink-0 rounded bg-black/40 px-1 text-[9px] uppercase tracking-wide text-gray-400">
        {row.kind}
      </span>
      <span className="min-w-0 break-all font-mono text-gray-300">{row.identity}</span>
      <span className={`shrink-0 rounded px-1 text-[9px] ${CHANGE_TONES[row.change]}`}>
        {row.change}
      </span>
      {same ? (
        <span className="min-w-0 break-all" title={row.before?.title}>
          {row.before?.text}
        </span>
      ) : (
        <span className="min-w-0 break-all">
          {row.before && (
            <span className="text-red-300" title={row.before.title}>
              {row.before.text}
            </span>
          )}
          {row.before && row.after && <span className="px-1 text-gray-600">→</span>}
          {row.after && (
            <span className="text-emerald-300" title={row.after.title}>
              {row.after.text}
            </span>
          )}
        </span>
      )}
      {row.note && <span className="min-w-0 break-words text-gray-500">{row.note}</span>}
    </li>
  );
}
