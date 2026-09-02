import type { PlaybookGraphNode } from "../../api/client";

/* The contract-derived intent payload the graph-view response attaches to each
 * contracted node. Declared here verbatim from the Package 1 child plan §3.6 so
 * this surface can be built and tested before the backend registration lands;
 * once `NodeExplanation` is part of the generated client this file becomes the
 * generated import plus an assignability check (child plan §11). Every
 * component in this directory imports the shape from here, never from a
 * component, so that swap stays a two-line edit. */

export interface ExplanationValue {
  kind: "literal" | "event_ref" | "binding_ref" | "loop_ref" | "template" | "unresolved";
  text: string;
  raw?: string | null;
  redacted?: boolean;
}

export interface ExplanationInput {
  field: string;
  label: string;
  value: ExplanationValue;
  required?: boolean;
}

export interface ExplanationEffect {
  operation: string;
  text: string;
  condition?: string | null;
  subject?: string | null;
}

export interface ExplanationOutcome {
  outcome: string;
  label: string;
  classification: "success" | "failure";
  target_node_id?: string | null;
  target_label?: string | null;
}

export interface ExplanationResultBinding {
  name: string;
  fields?: string[];
}

export interface ExplanationLoop {
  source_text: string;
  item_binding: string;
  source_raw?: string | null;
}

export interface NodeExplanation {
  kind: string;
  title: string;
  command?: string | null;
  contract_fingerprint?: string | null;
  capability?: string | null;
  effects?: ExplanationEffect[];
  inputs?: ExplanationInput[];
  result?: ExplanationResultBinding | null;
  outcomes?: ExplanationOutcome[];
  loop?: ExplanationLoop | null;
  idempotency?: string | null;
  retry?: string | null;
  unrendered_fields?: string[];
}

/** The generated `PlaybookGraphNode` widened by the additive `explanation`
 *  field. Dropped in favour of the generated model on the reconciliation. */
export type ExplainedPlaybookGraphNode = PlaybookGraphNode & {
  explanation?: NodeExplanation | null;
};

/** One effect as a single line: the rendered sentence, with its predicate as a
 *  dashed suffix. A null/absent condition means unconditional, so nothing is
 *  appended rather than the word "always" being invented. */
export function effectLine(effect: ExplanationEffect): string {
  return effect.condition ? `${effect.text} — ${effect.condition}` : effect.text;
}

/** One argument as `label → value.text`. `text` is the only user-facing
 *  rendering; `raw` is Advanced-view material and is never mixed in here, which
 *  is also what keeps a redacted value redacted. */
export function inputLine(input: ExplanationInput): string {
  return `${input.label} → ${input.value.text}`;
}
