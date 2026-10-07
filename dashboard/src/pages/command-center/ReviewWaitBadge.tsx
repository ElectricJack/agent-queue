import { DocumentMagnifyingGlassIcon } from "@heroicons/react/24/outline";
import { Link } from "react-router-dom";
import type { ReviewWait } from "@aq/ts-client";
import { REVIEW_STATE_TONE, reviewHref, reviewStateLabel, reviewStateShort } from "./reviewWaitFormat";

function toneOf(wait: ReviewWait): string {
  return REVIEW_STATE_TONE[wait.review_state] ?? REVIEW_STATE_TONE.withdrawn ?? "";
}

/** One line a hover or a screen reader gets for a wait. */
function describe(wait: ReviewWait): string {
  const verb = wait.blocking ? "Waiting on" : "Gated on";
  return `${verb} ${wait.review_kind} review ${wait.review_id} (${reviewStateLabel(wait.review_state)}): ${wait.review_title}`;
}

interface Props {
  waits: ReviewWait[];
  /** `chip` sits inline in a container's one fixed-height header row, which
   *  must not grow; `pill` is the card's second status pill (spec §1.1). */
  variant?: "chip" | "pill";
}

const VARIANT_CLASS = {
  chip: "flex max-w-[14rem] rounded border border-g-accent/60 bg-g-accent-soft px-1 text-[9px] text-g-accent-ink",
  pill: "relative inline-flex h-5 min-w-0 max-w-[10rem] rounded-md px-2 text-[11px] font-semibold",
} as const;

/** The pill: "Review <id> · <state>", the whole pill in the state's tone. */
function PillBody({ wait, more }: { wait: ReviewWait; more: number }) {
  return (
    <>
      <span className="shrink-0">Review</span>{" "}
      <span className="min-w-0 truncate font-g-mono underline decoration-current/50 underline-offset-2">{wait.review_id}</span>{" "}
      <span className="shrink-0">· {reviewStateShort(wait.review_state)}</span>
      {more > 0 && <>{" "}<span className="shrink-0">+{more}</span></>}
    </>
  );
}

/**
 * The "waiting on review" link: the first review this task waits on
 * (the API lists blocking waits first), its id and state, as a link to the
 * review page. A sibling of the card's open button, never inside it, so a tap
 * on a phone follows the link instead of opening the task; `nodrag nopan`
 * keeps the canvas from starting a drag or pan under the finger.
 */
export function ReviewWaitBadge({ waits, variant = "chip" }: Props) {
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
      className={`nodrag nopan ${VARIANT_CLASS[variant]} ${variant === "pill" ? toneOf(first) : ""} shrink-0 items-center gap-1 hover:brightness-110 focus-visible:outline focus-visible:outline-2 focus-visible:outline-g-accent`}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => { if (event.key !== "Escape") event.stopPropagation(); }}
    >
      {variant === "pill" ? <PillBody wait={first} more={more} /> : (
        <>
          <DocumentMagnifyingGlassIcon aria-hidden className="h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 truncate font-mono underline decoration-g-accent/60 underline-offset-2">{first.review_id}</span>
          <span className={`ml-auto shrink-0 rounded px-1 ${toneOf(first)}`}>{reviewStateLabel(first.review_state)}</span>
          {more > 0 && <span className="shrink-0 opacity-80">+{more}</span>}
        </>
      )}
    </Link>
  );
}
