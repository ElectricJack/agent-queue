"""Token tracking utilities for playbook execution.

Contains the :class:`DailyTokenTracker` for enforcing daily playbook token caps,
:func:`_estimate_tokens` for rough token counting, and :func:`_midnight_today`
for daily accounting windows.

Extracted from :mod:`src.playbooks.runner` — these are standalone utilities
with no dependency on the ``PlaybookRunner`` class.
"""

from __future__ import annotations

import datetime


# ---------------------------------------------------------------------------
# Daily token cap helper
# ---------------------------------------------------------------------------


def _midnight_today() -> float:
    """Return the Unix timestamp for midnight (00:00) of the current local day.

    Used by the daily playbook token cap (roadmap 5.2.8) to determine
    the start of the accounting window.
    """
    today = datetime.date.today()
    return datetime.datetime.combine(today, datetime.time.min).timestamp()


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def _estimate_tokens(*texts: str) -> int:
    """Rough token estimate (~4 chars per token) for budget tracking.

    This is intentionally approximate — the real token count comes from the
    LLM provider, but we don't have access to that at the executor level.
    The estimate is used for budget enforcement which is meant to be a
    guardrail, not an exact meter.
    """
    total_chars = sum(len(t) for t in texts if t)
    return max(1, total_chars // 4)


# ---------------------------------------------------------------------------
# Daily token tracking (global playbook cap, spec §6 Token Budget)
# ---------------------------------------------------------------------------


class DailyTokenTracker:
    """Track cumulative playbook token usage per calendar day.

    Used to enforce a global daily token cap (``max_daily_playbook_tokens``
    in config) across all playbook runs.  The tracker stores per-day totals
    and automatically resets when the date changes (at midnight by default,
    or at a configured ``reset_hour``).

    Thread-safety note: this class is *not* thread-safe but is designed for
    use in a single-threaded asyncio loop.
    """

    def __init__(self, *, reset_hour: int = 0) -> None:
        """Initialise the tracker.

        Parameters
        ----------
        reset_hour:
            Hour of day (0–23) when the daily counter resets.  Defaults to
            0 (midnight).
        """
        self._usage: dict[str, int] = {}
        self._reset_hour: int = reset_hour

    @property
    def reset_hour(self) -> int:
        return self._reset_hour

    @reset_hour.setter
    def reset_hour(self, value: int) -> None:
        self._reset_hour = value

    def _today_key(self, *, now: datetime.datetime | None = None) -> str:
        """Return the date key for the current accounting day.

        If *now* is provided it is used instead of ``datetime.datetime.now()``
        (useful for testing).
        """
        now = now or datetime.datetime.now()
        # Subtract reset_hour so that e.g. 02:00 with reset_hour=6 still
        # belongs to the previous calendar day.
        adjusted = now - datetime.timedelta(hours=self._reset_hour)
        return adjusted.strftime("%Y-%m-%d")

    def add_tokens(self, count: int, *, now: datetime.datetime | None = None) -> None:
        """Record *count* tokens for the current day."""
        key = self._today_key(now=now)
        self._usage[key] = self._usage.get(key, 0) + count

    def get_usage(self, *, now: datetime.datetime | None = None) -> int:
        """Return total tokens used today."""
        key = self._today_key(now=now)
        return self._usage.get(key, 0)
