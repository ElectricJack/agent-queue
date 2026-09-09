"""Parse the text of ``claude -p "/usage" --output-format json``.

A pure function over the probe's ``result`` string — no subprocess, no
database, no clock of its own (``now`` is passed in).  The probe that feeds
it lives in :mod:`src.providers.probe`.

The wording of that output is a CLI implementation detail that will change
under us, so the parser is written to be *quiet* about surprises rather than
clever:

* every limit line is read by one regex, and the scope inside the
  parentheses is carried through verbatim — ``all models``, ``Fable``,
  ``Opus`` or a name nobody has seen yet all round-trip untouched;
* a reset clause we cannot read costs only the clock, not the percentage;
* text with no limit line at all yields **zero** snapshots and
  ``unparsed=True``, so the caller keeps its previous reading.  A blank card
  beats a wrong number.

The one text without limit lines that is *not* a fault is the API-key
preamble: an account billed per token has no subscription window, which is
"not applicable" rather than "the parser broke".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.providers.snapshot import ProviderUsageSnapshot

__all__ = ["UsageParse", "parse_usage_text"]

PROVIDER = "claude"
SOURCE = "probe"

#: One limit line.  ``· resets ...`` is optional: a plan that stops printing
#: the clause should still give us the percentage.
_LIMIT_RE = re.compile(
    r"^Current\s+(?P<window>session|week\s*\((?P<scope>[^)]*)\))\s*:\s*"
    r"(?P<pct>\d+(?:\.\d+)?)\s*%\s+used"
    r"(?:\s*[·|-]\s*resets\s+(?P<reset>.+?))?\s*$",
    re.IGNORECASE,
)

#: An account that is billed per token has no window to report.  Absence of
#: limit lines there is an answer, not a failure.
_NOT_APPLICABLE_RE = re.compile(
    r"\b(api\s*key|pay[- ]as[- ]you[- ]go|credit\s+balance|console\.anthropic\.com)\b",
    re.IGNORECASE,
)

_RESET_RE = re.compile(
    r"^(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\s*,\s*"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[ap]\.?m\.?)?"
    r"(?:\s*\((?P<tz>[^)]+)\))?\s*$",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True, slots=True)
class UsageParse:
    """What one probe's text yielded.

    ``unparsed`` means "there was text and none of it made sense" — the
    signal ``aq doctor --check providers.claude_usage`` reports on.  It is
    *not* set merely because ``snapshots`` is empty; see the module
    docstring on the API-key case.
    """

    snapshots: list[ProviderUsageSnapshot] = field(default_factory=list)
    unparsed: bool = False


def parse_usage_text(
    text: str, *, now: float, tz_default: str = "UTC"
) -> UsageParse:
    """Read the limit lines out of ``/usage`` output.

    ``now`` is the observation epoch: it stamps every snapshot and anchors
    the year-less reset dates.  ``tz_default`` is used when a reset clause
    names no zone or names one this box does not have.
    """

    snapshots: list[ProviderUsageSnapshot] = []
    for line in text.splitlines():
        match = _LIMIT_RE.match(line.strip())
        if match is None:
            continue
        scope = match.group("scope")
        snapshots.append(
            ProviderUsageSnapshot(
                provider=PROVIDER,
                window="session" if scope is None else "week",
                scope="" if scope is None else scope.strip(),
                used_percent=float(match.group("pct")),
                resets_at=_parse_reset(match.group("reset"), now=now, tz_default=tz_default),
                observed_at=now,
                source=SOURCE,
            )
        )

    if snapshots:
        return UsageParse(snapshots=snapshots)
    return UsageParse(unparsed=_NOT_APPLICABLE_RE.search(text) is None)


def _parse_reset(prose: str | None, *, now: float, tz_default: str) -> float | None:
    """Resolve ``Sep 9, 2:59pm (America/Los_Angeles)`` to an epoch.

    The date carries no year, so we take the next occurrence at or after
    ``now``: a ``Dec 31`` seen on ``Jan 1`` is eleven months ahead, never
    yesterday.  Anything unreadable — a missing clause, an unknown zone we
    cannot even fall back from, a February 30th — costs the clock and
    nothing else.
    """

    if prose is None:
        return None
    match = _RESET_RE.match(prose.strip())
    if match is None:
        return None
    month = _MONTHS.get(match.group("month")[:3].lower())
    if month is None:
        return None

    hour = int(match.group("hour"))
    ampm = (match.group("ampm") or "").replace(".", "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23:
        return None

    tz = _zone(match.group("tz")) or _zone(tz_default) or _zone("UTC")
    if tz is None:  # pragma: no cover - UTC is always available
        return None

    base = datetime.fromtimestamp(now, tz).year
    for year in (base - 1, base, base + 1):
        try:
            moment = datetime(
                year, month, int(match.group("day")), hour, int(match.group("minute")), tzinfo=tz
            )
        except ValueError:
            continue
        epoch = moment.timestamp()
        if epoch >= now:
            return epoch
    return None


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
