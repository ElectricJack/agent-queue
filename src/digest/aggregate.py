"""One entry point, used by both the dry preview and the real delivery.

``build_digest`` is a pure function of its inputs: it performs no I/O, needs
no LLM, and reads no clock -- the window is given.  That is what lets the
dashboard's preview promise "this is what would be sent": preview and delivery
call the same function with the same inputs and get the same bytes.

The result also carries what the caller must persist to keep later windows
honest: the fact keys and highlight texts that were reported, and a hash of
the rendered output for the window's reserved output key.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from src.digest.eligibility import Eligibility, evaluate_eligibility, highlight_key
from src.digest.facts import CATEGORIES, DigestInputs
from src.digest.render import MAX_CHARS, MAX_HIGHLIGHTS, render_digest


@dataclass(frozen=True, slots=True)
class DigestResult:
    """Outcome of evaluating one window."""

    send: bool
    reason: str
    text: str = ""
    completed_count: int = 0
    active_count: int = 0
    #: Fact keys this digest reported; persist them so a later window (or a
    #: re-evaluation of this one) does not report them again.
    reported_keys: frozenset[str] = frozenset()
    #: Task-scoped normalised highlight wordings shown, for the same reason.
    reported_highlights: frozenset[str] = frozenset()
    eligibility: Eligibility | None = field(default=None, repr=False)

    @property
    def output_hash(self) -> str:
        """Stable digest of the rendered text, for the window's output key."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def build_digest(
    inputs: DigestInputs,
    *,
    project_ids: frozenset[str] | None = None,
    categories: frozenset[str] | None = None,
    dashboard_url: str = "",
    dashboard_notice: str = "",
    max_chars: int = MAX_CHARS,
    max_highlights: int = MAX_HIGHLIGHTS,
) -> DigestResult:
    """Evaluate ``inputs`` and render the message, or explain the silence."""
    if categories is not None:
        unknown = categories - set(CATEGORIES)
        if unknown:
            raise ValueError(f"unknown digest categories: {sorted(unknown)}")

    eligibility = evaluate_eligibility(
        inputs, project_ids=project_ids, categories=categories
    )
    if not eligibility.send:
        return DigestResult(
            send=False, reason=eligibility.reason, eligibility=eligibility
        )

    text = render_digest(
        eligibility,
        inputs.window,
        project_names=inputs.project_names,
        dashboard_url=dashboard_url,
        dashboard_notice=dashboard_notice,
        open_escalations=inputs.open_escalations,
        max_chars=max_chars,
        max_highlights=max_highlights,
    )
    highlights = frozenset(highlight_key(fact) for fact in eligibility.facts)
    return DigestResult(
        send=True,
        reason=eligibility.reason,
        text=text,
        completed_count=eligibility.completed_count,
        active_count=eligibility.active_count,
        reported_keys=frozenset(fact.key for fact in eligibility.facts),
        reported_highlights=highlights,
        eligibility=eligibility,
    )
