"""Supervisor-authored digest policy: what a window says, and when it may say it.

Phase P3 of *Discord as a chat extension of the supervisor* (2026-10-03 §4).
The daemon owns cadence, budgets, delivery, state and links; the supervisor owns
the three sentences.  This module is the whole of the daemon's half that is not
already :mod:`src.digest.dispatch`: it is pure, clock-free and transport-free, so
every rule below is a table a test can read rather than a code path to trace.

Four rules, and each one exists because the channel is append-only:

* **§4.1 cadence and quiet hours.**  Windows sit on the cadence grid and one
  window is one durable row, so a restart cannot double-post.  Quiet hours
  suppress the *post*, never the fact collection: the night still has facts, and
  the 07:00 morning report covers it.
* **§4.3 "nothing to report".**  A window with no activity whose facts hash
  equals the previous window's, and nothing waiting on Jack, is skipped -- an
  unchanged fleet is not news.  Three consecutive skips earn one line a day, so
  silence stays distinguishable from a dead bot.
* **§4.2 fallback.**  A window that does speak is *held* for
  ``author_fallback_minutes`` after it closes.  If ``aq digest post`` has not
  written the body by then, the deterministic digest for the same facts goes out
  instead: the cadence never silently stops because the supervisor was busy.
* **§3.2 the size budget.**  The body is rendered here, inside
  :data:`~src.discord.render_budget.DIGEST` -- three lines, 600 characters, one
  link, appended by the daemon.  An author cannot post a link, a mention or a
  wall of text through this path, because it is a renderer and not a channel.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from src.config import ReportQuietHoursConfig
from src.dashboard_paths import inbox_path
from src.digest.render import sanitise
from src.discord.render_budget import (
    DIGEST,
    GLYPH_DIGEST,
    GLYPH_WORKING,
    GLYPHS,
    clip_section,
    compose,
    link_line,
)

#: What one window does.  Stable identifiers: the dashboard and
#: ``digest_status`` render them and tests assert on them.
HOLD = "hold"
SKIP_UNCHANGED = "unchanged"
SKIP_QUIET_HOURS = "quiet_hours"
SEND_QUIET_LINE = "quiet_line"

#: §4.3's one-a-day line, word for word, with the session count substituted.
QUIET_LINE = "Quiet: {sessions} sessions working, nothing needs you."

#: Caps on the frozen brief.  Each bounds a *list*, never the counts beside it:
#: the author needs the shape of the window, and every cap says what it dropped.
MAX_LANDED = 10
MAX_STUCK = 10
MAX_NEEDS_YOU = 5

#: A task blocked this long is worth naming as stuck rather than as ordinary
#: waiting.  Mechanism, not policy: the supervisor may still call it normal in
#: the sentence it writes.
STUCK_BLOCKED_MINUTES = 30.0


def facts_hash(facts: dict[str, Any]) -> str:
    """Stable identity of one window's facts, for §4.3's change test.

    Hashes the identities behind the counts, never the prose: a re-worded fact
    must not read as progress, and a window whose set did not change is not news
    however it is worded.
    """
    needs = facts.get("needs_you") or {}
    material = json.dumps(
        {
            "landed": [
                fact.get("task_id", "") for fact in facts.get("landed", {}).get("items", [])
            ],
            "stuck": [
                (fact.get("task_id", ""), fact.get("reason", ""))
                for fact in facts.get("stuck", {}).get("items", [])
            ],
            "escalations": [
                item.get("id", "") for item in needs.get("escalations", {}).get("items", [])
            ],
            "reviews": [item.get("id", "") for item in needs.get("reviews", {}).get("items", [])],
            "counts": facts.get("counts", {}),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def needs_you_count(facts: dict[str, Any]) -> int:
    """How many things in this window are waiting on Jack."""
    needs = facts.get("needs_you") or {}
    return len(needs.get("escalations", {}).get("items", [])) + len(
        needs.get("reviews", {}).get("items", [])
    )


def quiet_at(now: float, timezone: str, quiet: ReportQuietHoursConfig | None) -> bool:
    """Whether *now* falls inside the configured local quiet interval.

    The same interval arithmetic the hourly report author uses, evaluated in the
    daemon's report timezone rather than UTC: "22:00-07:00" means the hours a
    person is asleep, wherever the box is.
    """
    if quiet is None:
        return False
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    clock = local.hour * 60 + local.minute
    start = int(quiet.start[:2]) * 60 + int(quiet.start[3:])
    end = int(quiet.end[:2]) * 60 + int(quiet.end[3:])
    if start < end:
        return start <= clock < end
    # An interval that wraps midnight is two ranges, not a wrap-around compare.
    return clock >= start or clock < end


def author_deadline(window_end: float, fallback_seconds: float) -> float:
    """When §4.2's fallback fires: the window's close plus the grace window."""
    return float(window_end) + float(fallback_seconds)


@dataclass(frozen=True, slots=True)
class WindowDecision:
    """What one window does, and why -- §4.1 and §4.3 as a value."""

    #: One of :data:`HOLD`, :data:`SEND_QUIET_LINE`, :data:`SKIP_UNCHANGED`,
    #: :data:`SKIP_QUIET_HOURS`.
    action: str
    reason: str
    #: Consecutive skipped windows *including* this one; the once-a-day line
    #: spends it, because a line is not a skip.
    consecutive_skips: int = 0
    quiet_line_due: bool = False

    @property
    def posts(self) -> bool:
        """Whether the window itself produces a message now."""
        return self.action == SEND_QUIET_LINE

    @property
    def skips(self) -> bool:
        return self.action in (SKIP_UNCHANGED, SKIP_QUIET_HOURS)


def decide_window(
    *,
    facts: dict[str, Any],
    has_activity: bool,
    previous_facts_hash: str | None,
    previous_quiet_line_day: str | None,
    consecutive_skips: int,
    quiet_now: bool,
    now: float,
    timezone: str,
    quiet_line_after: int,
) -> WindowDecision:
    """§4.1 + §4.3 for one window: post it, hold it for its author, or stay silent.

    Order matters and it is the spec's order.  Quiet hours come first: nothing is
    posted overnight whatever the facts say, and a quiet hour is not a "nothing to
    report" skip -- it must not spend the once-a-day line or count toward the
    three that earn it.  Only then is an unchanged fleet compared with the previous
    window's hash, and only then does anything needing Jack outrank the silence.
    """
    if quiet_now:
        return WindowDecision(action=SKIP_QUIET_HOURS, reason=SKIP_QUIET_HOURS)
    unchanged = bool(previous_facts_hash) and previous_facts_hash == facts_hash(facts)
    if not unchanged and (has_activity or needs_you_count(facts) > 0):
        return WindowDecision(action=HOLD, reason="facts_changed", consecutive_skips=0)
    # Either the window says exactly what the last one said -- including the
    # same escalations still waiting, whose ids are part of the hash -- or there
    # is nothing in it at all.  Both are silence.
    skips = consecutive_skips + 1
    day = datetime.fromtimestamp(now, ZoneInfo(timezone)).date().isoformat()
    # §4.3: after N consecutive skipped windows one line is allowed per day, so
    # an idle fleet stays distinguishable from a dead bot.
    due = skips >= quiet_line_after and previous_quiet_line_day != day
    return WindowDecision(
        action=SEND_QUIET_LINE if due else SKIP_UNCHANGED,
        reason=SKIP_UNCHANGED,
        consecutive_skips=0 if due else skips,
        quiet_line_due=due,
    )


def quiet_line(sessions_working: int, *, base_url: str = "", notice: str = "") -> str:
    """The §4.3 one-line "still alive, nothing needs you" post."""
    line = f"{GLYPH_WORKING} {QUIET_LINE.format(sessions=int(sessions_working))}"
    return compose([line], DIGEST, link=link_line(base_url, inbox_path(), notice=notice))


def render_authored_digest(
    body: str,
    *,
    base_url: str = "",
    notice: str = "",
    marker: str = "",
) -> str:
    """The post for ``aq digest post``: the supervisor's words inside §3.2.

    A body longer than the budget is *cut* rather than refused: the window still
    gets its one digest and the link carries the rest, so a verbose author
    degrades the post instead of losing it.  §3.1's glyph is added only when the
    author did not lead with one, and every line is sanitized -- an author must
    not be able to ping a role from a routine digest.
    """
    # The authored body is free prose, not the deterministic digest's sections:
    # §3.2's three body lines are what a landed / stuck / needs-you answer
    # spends, so that is the cap here and ``section_lines`` stays the renderer
    # rule for the sections it was written for.
    body = clip_section(body, DIGEST.body_lines or 0)
    sections = [clean for raw in body.splitlines() if (clean := sanitise(raw))]
    if not sections:
        raise ValueError("digest body is empty after sanitizing")
    if sections[0][0] not in GLYPHS:
        sections[0] = f"{GLYPH_DIGEST} {sections[0]}"
    return compose(
        sections,
        DIGEST,
        link=link_line(base_url, inbox_path(), notice=notice),
        marker=marker,
    )


def fallback_text(deterministic: str, facts: dict[str, Any]) -> str:
    """The message §4.2 posts when no ``digest post`` arrives in time.

    The deterministic digest for the same facts is the fallback, unchanged: it is
    the render that has been running all along, and reusing it is what makes the
    flag's rollback path identical to today's behaviour.  A window the
    deterministic renderer declined -- which can still be *held* when something
    needs Jack and nothing else moved -- needs a fallback that says that much, so
    the one-line needs-you form is composed from the frozen counts rather than
    invented prose.
    """
    if deterministic.strip():
        return deterministic
    counts = facts.get("counts", {})
    needs = int(counts.get("needs_you") or 0)
    working = int(counts.get("sessions_working") or 0)
    if needs:
        noun = "item" if needs == 1 else "items"
        line = f"{GLYPH_DIGEST} Needs you: {needs} waiting {noun}; {working} sessions working."
    else:
        line = f"{GLYPH_DIGEST} {working} sessions working."
    return compose([line], DIGEST)


def _capped(rows: list[dict[str, Any]], cap: int) -> dict[str, Any]:
    return {"items": rows[:cap], "omitted": max(0, len(rows) - cap)}


def bounded_facts(
    *,
    window: Any,
    landed: list[dict[str, Any]],
    stuck: list[dict[str, Any]],
    escalations: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    sessions_working: int,
    sessions_total: int,
    active_tasks: int,
    open_escalations: int,
    overdue_deliveries: int = 0,
    previous_facts_hash: str | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """The frozen brief for one window: counts exact, lists capped and counted."""
    return {
        "window": {
            "since": float(window.since),
            "until": float(window.until),
            "deadline": deadline,
        },
        "counts": {
            "landed": len(landed),
            "stuck": len(stuck),
            "needs_you": len(escalations) + len(reviews),
            "sessions_working": int(sessions_working),
            "active_tasks": int(active_tasks),
            "open_escalations": int(open_escalations),
            "overdue_deliveries": int(overdue_deliveries),
        },
        "landed": _capped(landed, MAX_LANDED),
        "stuck": _capped(stuck, MAX_STUCK),
        "needs_you": {
            "escalations": _capped(escalations, MAX_NEEDS_YOU),
            "reviews": _capped(reviews, MAX_NEEDS_YOU),
        },
        "sessions": {"working": int(sessions_working), "total": int(sessions_total)},
        "previous_facts_hash": previous_facts_hash or "",
        "budget": {
            "chars": DIGEST.chars,
            "body_lines": DIGEST.body_lines,
            "section_lines": DIGEST.section_lines,
            "link": inbox_path(),
        },
    }


__all__ = [
    "HOLD",
    "MAX_LANDED",
    "MAX_NEEDS_YOU",
    "MAX_STUCK",
    "QUIET_LINE",
    "SEND_QUIET_LINE",
    "SKIP_QUIET_HOURS",
    "SKIP_UNCHANGED",
    "STUCK_BLOCKED_MINUTES",
    "WindowDecision",
    "author_deadline",
    "bounded_facts",
    "clip_section",
    "decide_window",
    "facts_hash",
    "fallback_text",
    "needs_you_count",
    "quiet_at",
    "quiet_line",
    "render_authored_digest",
]
