"""Process exit → typed verdict.

Under the session runtime, **process exit with the task still open is a
failure signal**, never success.  Completion is `aq task close` followed by
`aq session drain-ack`, and nothing else.  This module turns "the process is
gone" into one of four verdicts the reconciler knows how to apply.

The point is not that classification is easy — it is that it happens in one
place, on positive evidence, and produces a value the caller can switch on.
The old runtime inferred all of this from string-matching error messages
scattered across the result branch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["ExitVerdict", "Verdict", "classify_exit", "RATE_LIMIT_PATTERNS"]


class Verdict(StrEnum):
    """What the exit means."""

    #: Provider rate limit hit.  Task → PAUSED with a cooldown; the session
    #: is *not* restarted straight back into the limit.
    RATE_LIMIT = "rate_limit"
    #: Died shortly after start — a launch or config problem, not work.
    #: Restart with backoff; quarantine once the ladder is exhausted.
    RAPID_CRASH = "rapid_crash"
    #: Ran for a while, then exited with the task still open.  Never
    #: silently READY: needs_attention or a policy-driven re-queue.
    PRODUCTIVE_DEATH = "productive_death"
    #: The task was already closed — this is the normal drain path.
    DRAINED = "drained"


@dataclass(frozen=True)
class ExitVerdict:
    """A classification plus the evidence that produced it."""

    verdict: Verdict
    reason: str = ""
    #: Seconds the caller should wait before any restart, when applicable.
    cooldown_seconds: float = 0.0

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return f"{self.verdict}({self.reason})"


#: Substrings/regexes that identify a provider rate limit in the last pane
#: capture.  Matched case-insensitively.  This is pane *text* — a hint, not
#: a structured channel — so it is used only to choose between "pause with a
#: cooldown" and "treat as a crash", both of which are safe.
RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    r"rate.?limit",
    r"usage limit reached",
    r"approaching your usage limit",
    r"quota exceeded",
    r"429",
    r"too many requests",
    r"overloaded_error",
    r"resets at",
)

_RATE_LIMIT_RE = re.compile("|".join(RATE_LIMIT_PATTERNS), re.IGNORECASE)

#: Terminal task statuses — if the task reached one of these, the session
#: lingering is just a drain, whatever the exit looked like.
_CLOSED_STATUSES = frozenset({"completed", "failed", "cancelled", "archived"})


def _status_str(task) -> str:
    status = getattr(task, "status", None)
    if status is None:
        return ""
    return str(getattr(status, "value", status)).lower()


def classify_exit(
    session,
    task,
    last_peek: str = "",
    *,
    now: float,
    rapid_crash_window: float = 600.0,
    rate_limit_cooldown: float = 900.0,
) -> ExitVerdict:
    """Classify a dead session.

    Order matters.  ``DRAINED`` is checked first because a closed task makes
    every other question moot; ``RATE_LIMIT`` outranks ``RAPID_CRASH``
    because restarting into a rate limit burns the restart budget without
    ever making progress.

    Parameters
    ----------
    session:
        The :class:`~src.models.SessionRecord` whose process is gone.
    task:
        The task it was running, or ``None`` for a named session.
    last_peek:
        Final pane capture, when the provider can produce one.  Empty for
        pane-less providers — classification degrades to timing, which is
        why the timing rule exists at all.
    now:
        Injected rather than read from the clock so tests are not timing
        races (the Windows clock ticks in ~15 ms steps, which has already
        cost this repo a flaky test).
    """
    status = _status_str(task) if task is not None else ""
    if task is None or status in _CLOSED_STATUSES:
        return ExitVerdict(Verdict.DRAINED, "task already closed" if task is not None else "named session")

    if last_peek and _RATE_LIMIT_RE.search(last_peek):
        return ExitVerdict(
            Verdict.RATE_LIMIT,
            "rate-limit text in final capture",
            cooldown_seconds=rate_limit_cooldown,
        )

    age = max(0.0, now - (session.started_at or now))
    if age < rapid_crash_window:
        return ExitVerdict(
            Verdict.RAPID_CRASH,
            f"died {age:.0f}s after start (< {rapid_crash_window:.0f}s window)",
        )

    return ExitVerdict(
        Verdict.PRODUCTIVE_DEATH,
        f"exited after {age:.0f}s with the task still open",
    )
