import { DocumentMagnifyingGlassIcon } from "@heroicons/react/24/outline";
import { Link } from "react-router-dom";
import type { ReviewWait } from "@aq/ts-client";
import { REVIEW_STATE_TONE, reviewHref, reviewStateLabel } from "./reviewWaitFormat";

/** One line a hover or a screen reader gets for a wait. */
function describe(wait: ReviewWait): string {
  const verb = wait.blocking ? "Waiting on" : "Gated on";
  return `${verb} ${wait.review_kind} review ${wait.review_id} (${reviewStateLabel(wait.review_state)}): ${wait.review_title}`;
}

interface Props {
  waits: ReviewWait[];
  /** `strip` is the card's bottom row; `chip` sits inline in a container's
   *  one fixed-height header row, which must not grow. */
  variant?: "strip" | "chip";
}

const VARIANT_CLASS = {
  strip: "flex min-h-7 border-t border-violet-400/40 px-2 text-[10px]",
  chip: "flex max-w-[14rem] rounded border border-violet-400/60 px-1 text-[9px]",
} as const;

/**
 * The "waiting on review" link: the first review this task waits on
 * (the API lists blocking waits first), its id and state, as a link to the
 * review page. A sibling of the card's open button, never inside it, so a tap
 * on a phone follows the link instead of opening the task; `nodrag nopan`
 * keeps the canvas from starting a drag or pan under the finger.
 */
export function ReviewWaitBadge({ waits, variant = "strip" }: Props) {
  const first = waits[0];
  if (!first) return null;
  const more = waits.length - 1;
  const title = waits.map(describe).join("\n");
  return (
    <Link
      to={reviewHref(first.review_id)}
      data-review-wait={first.blocking ? "blocking" : "released"}
      aria-label={more > 0 ? `${describe(first)} (${more} more)` : describe(first)}
      title={title}
      className={`nodrag nopan ${VARIANT_CLASS[variant]} shrink-0 items-center gap-1 bg-violet-950/70 text-violet-100 hover:bg-violet-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-violet-300`}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => { if (event.key !== "Escape") event.stopPropagation(); }}
    >
      <DocumentMagnifyingGlassIcon aria-hidden className="h-3.5 w-3.5 shrink-0" />
      {variant === "strip" && <span className="shrink-0 font-semibold">{first.blocking ? "Awaiting review" : "Reviewed"}</span>}
      <span className="min-w-0 truncate font-mono underline decoration-violet-300/60 underline-offset-2">{first.review_id}</span>
      <span className={`ml-auto shrink-0 rounded px-1 ${REVIEW_STATE_TONE[first.review_state] ?? REVIEW_STATE_TONE.withdrawn}`}>
        {reviewStateLabel(first.review_state)}
      </span>
      {more > 0 && <span className="shrink-0 opacity-80">+{more}</span>}
    </Link>
  );
}
