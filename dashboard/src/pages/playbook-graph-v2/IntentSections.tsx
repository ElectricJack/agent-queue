import type { ReactNode } from "react";
import type {
  EffectClauseDTO,
  ExplanationRowDTO,
  ExplanationValueDTO,
  GraphNodeDTO,
  StepExplanationDTO,
} from "../../api/client";

/** Glyphs for the twelve declared effect kinds. Decoration only — the sentence
 *  beside it carries the whole meaning, so an unmapped kind degrades to a
 *  neutral bullet rather than hiding the effect. */
const EFFECT_GLYPHS: Record<string, string> = {
  creates: "＋",
  updates: "✎",
  deletes: "－",
  reads: "👁",
  sends: "✉",
  schedules: "⏱",
  waits: "⏸",
  branches: "⑂",
  binds: "⇥",
  invokes_ai: "✦",
  delegates: "⇄",
  noop: "·",
};

/** The one line a compact card shows under the title. Every branch reads the
 *  explanation payload or a typed detail block — never `advanced.typed_step`,
 *  which is the Advanced disclosure's data and is not what the card explains. */
export function secondaryLine(node: GraphNodeDTO): string | null {
  const explanation = node.explanation;
  switch (node.step_kind) {
    case "llm": {
      const declared = (explanation.outcomes ?? []).filter((o) => !o.reserved);
      const choices = (declared.length > 0 ? declared : (explanation.outcomes ?? [])).map(
        (o) => o.label || o.outcome,
      );
      return choices.length > 0 ? choices.join(", ") : explanation.effect_summary;
    }
    case "decision": {
      const cases = (explanation.outcomes ?? []).length;
      return `${explanation.effect_summary} — ${cases} case${cases === 1 ? "" : "s"}`;
    }
    case "wait":
      return node.wait ? `${node.wait.wait_kind}: ${node.wait.awaited}` : explanation.effect_summary;
    case "foreach":
      return node.loop
        ? `${node.loop.collection.display} → ${node.loop.item_binding}`
        : explanation.effect_summary;
    case "terminal":
      return node.terminal_outcome ?? explanation.effect_summary;
    default:
      return explanation.effect_summary || null;
  }
}

export function Block({ name, children }: { name: string; children: ReactNode }) {
  return (
    <section role="group" aria-label={name} className="min-w-0 space-y-1">
      <h5 className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">{name}</h5>
      {children}
    </section>
  );
}

/** A value is rendered from `display` alone. `canonical` belongs to the
 *  Advanced disclosure, and is absent entirely whenever the backend redacted
 *  the value — there is no client-side redaction in this package. */
export function Value({ value }: { value: ExplanationValueDTO }) {
  return (
    <span
      title={value.type_name ? `${value.type_name} (${value.kind})` : value.kind}
      className={`min-w-0 break-words ${value.redacted ? "font-mono text-rose-300" : "text-gray-200"}`}
    >
      {value.display}
    </span>
  );
}

export function Rows({ rows }: { rows: ExplanationRowDTO[] }) {
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
      {rows.map((row, index) => (
        <div key={`${row.label}:${index}`} className="contents">
          <dt className="flex items-baseline gap-1 text-gray-500" title={row.description ?? undefined}>
            <span className="break-words">{row.label}</span>
            <span className="shrink-0 rounded bg-gray-800 px-1 text-[9px] uppercase tracking-wide text-gray-400">
              {row.source}
            </span>
            {row.required === false && (
              <span className="shrink-0 text-[9px] uppercase tracking-wide text-gray-600">optional</span>
            )}
          </dt>
          <dd className="min-w-0">
            <Value value={row.value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Effect({ effect }: { effect: EffectClauseDTO }) {
  return (
    <li className="min-w-0 space-y-1 text-xs">
      <p className="flex min-w-0 items-baseline gap-2">
        <span aria-hidden className="shrink-0 text-gray-500">
          {EFFECT_GLYPHS[effect.kind] ?? "•"}
        </span>
        <span className="min-w-0 break-words text-gray-200">
          {effect.detail}
          {effect.conditional_on && (
            <span className="text-gray-500"> (when {effect.conditional_on})</span>
          )}
        </span>
      </p>
      {(effect.arguments ?? []).length > 0 && (
        <div className="pl-6">
          <Rows rows={effect.arguments!} />
        </div>
      )}
    </li>
  );
}

export interface IntentSectionsProps {
  explanation: StepExplanationDTO;
}

/** The contract-derived intent of one step. The compact card and the inspector
 *  consume the same payload through this module, so the two surfaces can never
 *  disagree about what a step does. */
export default function IntentSections({ explanation }: IntentSectionsProps) {
  const effects = explanation.effects ?? [];
  const inputs = explanation.inputs ?? [];
  const outcomes = explanation.outcomes ?? [];

  return (
    <div className="min-w-0 space-y-3">
      <p className="min-w-0 break-words text-xs text-gray-300">{explanation.effect_summary}</p>

      {explanation.renderer === "canonical" && (
        <p
          role="status"
          className="rounded border border-amber-700 bg-amber-950/60 px-2 py-1 text-[10px] text-amber-200"
        >
          No presentation metadata for this step — every executable field is shown verbatim.
        </p>
      )}

      {effects.length > 0 && (
        <Block name="Effects">
          <ul className="space-y-1.5">
            {effects.map((effect, index) => (
              <Effect key={`${effect.kind}:${index}`} effect={effect} />
            ))}
          </ul>
        </Block>
      )}

      {inputs.length > 0 && (
        <Block name="Inputs">
          <Rows rows={inputs} />
        </Block>
      )}

      {explanation.result && (
        <Block name="Result">
          <Rows rows={[explanation.result]} />
        </Block>
      )}

      {outcomes.length > 0 && (
        <Block name="Outcomes">
          <ul className="space-y-1">
            {outcomes.map((outcome) => (
              <li
                key={outcome.outcome}
                className="flex min-w-0 items-baseline gap-2 rounded border border-gray-800 px-2 py-1 text-xs"
              >
                <span className={`shrink-0 ${outcome.reserved ? "text-gray-400" : "text-emerald-300"}`}>
                  {outcome.label || outcome.outcome}
                </span>
                {outcome.reserved && (
                  <span className="shrink-0 rounded bg-gray-800 px-1 text-[9px] uppercase tracking-wide text-gray-500">
                    reserved
                  </span>
                )}
                <span aria-hidden className="text-gray-600">
                  →
                </span>
                <span className="min-w-0 break-words text-gray-200">
                  {outcome.terminal_outcome
                    ? `ends the rule as ${outcome.terminal_outcome}`
                    : (outcome.target_title ?? outcome.target_step_id ?? "ends the rule")}
                </span>
              </li>
            ))}
          </ul>
        </Block>
      )}
    </div>
  );
}
