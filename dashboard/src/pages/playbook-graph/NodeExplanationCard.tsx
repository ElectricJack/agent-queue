import type { ReactNode } from "react";
import { effectLine, inputLine, type NodeExplanation } from "./explanation";

/** Glyphs for the declared effect operations. Decoration only — the sentence
 *  next to it carries the whole meaning, so an unmapped operation degrades to a
 *  neutral bullet rather than hiding the effect. */
const OPERATION_GLYPHS: Record<string, string> = {
  create: "＋",
  reuse: "↺",
  create_or_reuse: "⊕",
  update: "✎",
  link: "⇄",
  resolve: "✓",
  read: "👁",
};

function Block({ name, children }: { name: string; children: ReactNode }) {
  return (
    <section role="group" aria-label={name} className="min-w-0 space-y-1">
      <h5 className="text-[10px] font-semibold uppercase tracking-wide text-gray-500">{name}</h5>
      {children}
    </section>
  );
}

export interface NodeExplanationCardProps {
  explanation?: NodeExplanation | null;
}

/** The contract-derived intent of one node, as prose the operator can read
 *  without knowing the compiled argument syntax.
 *
 *  Everything rendered here comes from the explanation payload; the card never
 *  parses the compiled action, and never renders `ExplanationValue.raw` — the
 *  raw expressions stay in the inspector's Advanced disclosure, which is what
 *  keeps a value the contract marked sensitive redacted on this surface. */
export default function NodeExplanationCard({ explanation }: NodeExplanationCardProps) {
  if (!explanation) return null;

  const effects = explanation.effects ?? [];
  const inputs = explanation.inputs ?? [];
  const outcomes = explanation.outcomes ?? [];
  const unrendered = explanation.unrendered_fields ?? [];
  const resultFields = explanation.result?.fields ?? [];
  const guarantees = [explanation.idempotency, explanation.retry].filter(
    (line): line is string => Boolean(line),
  );

  return (
    <div className="min-w-0 space-y-3 rounded border border-gray-800 bg-gray-950 p-3">
      <div className="min-w-0 space-y-0.5">
        <h4 className="break-words text-sm font-semibold text-gray-100">{explanation.title}</h4>
        {explanation.command && (
          <p className="break-all font-mono text-[11px] text-gray-500">{explanation.command}</p>
        )}
      </div>

      {effects.length > 0 && (
        <Block name="Effects">
          <ul className="space-y-1">
            {effects.map((effect, index) => (
              <li
                key={`${effect.operation}:${index}`}
                title={effectLine(effect)}
                className="flex min-w-0 items-baseline gap-2 text-xs"
              >
                <span aria-hidden className="shrink-0 text-gray-500">
                  {OPERATION_GLYPHS[effect.operation] ?? "•"}
                </span>
                <span className="min-w-0 break-words text-gray-200">
                  {effect.text}
                  {effect.condition && (
                    <>
                      {" "}
                      <span className="text-gray-500">{effect.condition}</span>
                    </>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </Block>
      )}

      {explanation.loop && (
        <Block name="Repeats for">
          <p className="min-w-0 break-words text-xs text-gray-200">
            {explanation.loop.source_text}
            <span className="text-gray-500"> as </span>
            <span className="font-mono text-amber-200">{explanation.loop.item_binding}</span>
          </p>
        </Block>
      )}

      {inputs.length > 0 && (
        <Block name="Inputs">
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
            {inputs.map((input) => (
              <div key={input.field} className="contents">
                <dt className="flex items-baseline gap-1 text-gray-500">
                  <span className="break-words">{input.label}</span>
                  {input.required && (
                    <span className="shrink-0 rounded bg-gray-800 px-1 text-[9px] uppercase tracking-wide text-gray-400">
                      required
                    </span>
                  )}
                </dt>
                <dd
                  title={inputLine(input)}
                  className={`min-w-0 break-words ${input.value.redacted ? "font-mono text-rose-300" : "text-gray-200"}`}
                >
                  {input.value.text}
                </dd>
              </div>
            ))}
          </dl>
        </Block>
      )}

      {explanation.result && (
        <Block name="Result">
          <p className="text-xs text-gray-200">{`Save as "${explanation.result.name}"`}</p>
          {resultFields.length > 0 && (
            <ul className="flex flex-wrap gap-1">
              {resultFields.map((field) => (
                <li
                  key={field}
                  className="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-[10px] text-gray-300"
                >
                  {field}
                </li>
              ))}
            </ul>
          )}
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
                <span
                  className={`shrink-0 ${outcome.classification === "success" ? "text-emerald-300" : "text-rose-300"}`}
                >
                  {outcome.label}
                </span>
                {outcome.target_label && (
                  <>
                    <span aria-hidden className="text-gray-600">
                      →
                    </span>
                    <span className="min-w-0 break-all font-mono text-gray-200">
                      {outcome.target_label}
                    </span>
                  </>
                )}
              </li>
            ))}
          </ul>
        </Block>
      )}

      {unrendered.length > 0 && (
        <Block name="Other fields">
          <ul className="flex flex-wrap gap-1">
            {unrendered.map((field) => (
              <li
                key={field}
                className="rounded bg-gray-800 px-1.5 py-0.5 font-mono text-[10px] text-gray-300"
              >
                {field}
              </li>
            ))}
          </ul>
        </Block>
      )}

      {guarantees.length > 0 && (
        <Block name="Guarantees">
          <ul className="space-y-0.5">
            {guarantees.map((line) => (
              <li key={line} className="min-w-0 break-words text-[11px] text-gray-400">
                {line}
              </li>
            ))}
          </ul>
        </Block>
      )}
    </div>
  );
}
