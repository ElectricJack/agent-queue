import type { NodeExplanation, PlaybookGraphNode } from "../../api/client";
import type { ExplanationEffect, ExplanationInput } from "../../api/client";

/* The contract-derived intent payload the graph-view response attaches to each
 * contracted node.
 *
 * The shapes are the GENERATED client models — the same Pydantic definitions in
 * `src/api/models/playbook.py` that the backend renders from its command
 * contracts. This file hand-declared them while the backend registration was
 * still landing (Package 1 child plan §3.6, §11); keeping that copy would mean
 * a second, unversioned definition of a wire type the server owns, and the two
 * could disagree with nothing failing. Components import the shape from here
 * rather than from the client directly, so this stays the one seam. */

export type {
  ExplanationEffect,
  ExplanationInput,
  ExplanationLoop,
  ExplanationOutcome,
  ExplanationResultBinding,
  ExplanationValue,
  NodeExplanation,
} from "../../api/client";

/** A graph node carrying its explanation. `PlaybookGraphNode.explanation` is
 *  part of the generated model now, so this is a name, not a widening. */
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
