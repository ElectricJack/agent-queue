"""Is a stalled session's pane its CLI parked on a usage-limit screen?

A harness that hits its provider's usage limit mid-task usually does not
exit.  Claude Code prints ``You've hit your session limit · resets 1:40am
(America/Los_Angeles)`` and Codex ``You’ve hit your usage limit. … try again
at 3:44 PM.``, and both then sit at their prompt.  The exit classifier never
sees that session, because nothing died; only the stall ladder does, and
nudging a CLI that cannot answer just burns ~23 minutes before a restart.

This module is the stall ladder's evidence that it is looking at that screen
rather than at an agent that went quiet.  It is **deliberately strict**, the
opposite of :data:`~src.sessions.exit_classifier.RATE_LIMIT_PATTERNS`: that set
only chooses between two safe verdicts for a process that is already dead, so
a bare ``429`` or ``rate limit`` is good enough there.  Here a match stops a
live process, spends a WIP commit and a relaunch, and records provider
evidence — so it must not fire on ordinary agent output that merely mentions a
rate limit, nor on a diff or file view that quotes the limit wording (an agent
working on this very module would print it).

What makes a line count:

* it is one of the **last** :data:`USAGE_LIMIT_TAIL_LINES` non-blank lines of
  the pane (Claude pads the screen with blank rows below the message, so blank
  lines are not counted);
* it opens with the CLI's own gutter glyph at the left margin — Claude's
  ``⎿`` at column 2 (how it paints every API error) or Codex's ``■`` error
  cell at column 0 — and then, after whitespace (NBSP included), **starts**
  with one of the blocking wordings below.  A line that starts with anything
  else (``+`` of a diff, a ``path:line:`` grep hit, a composer ``❯``/``›``, a
  markdown ``-``/``>``) is output that quotes the wording, not the screen.  So
  is a line indented further: Claude paints the continuation lines of a
  tool's output five columns in, glyph or not, which is where an agent
  grepping for these strings — or cat-ing a saved pane — would print them.

Replayed over every Claude transcript on the box where this was written
(2,632 files, 2.5M lines rendered with the TUI's gutters), this matched all
104 real limit messages and nothing else.

OpenCode's retry footer is matched only when the caller has resolved that
CLI from the harness registry. Its spinner can repaint the countdown while
no work progresses, so callers inspect it before trusting pane activity.
Explicit free-usage exhaustion carries stronger evidence than an ordinary
server retry; the latter counts only for waits of at least an hour.

The wordings were taken from the CLIs themselves on 2026-09-21: Claude Code
2.1.278 builds every blocking message from the prefixes ``You've hit your``,
``You've reached your``, ``You're out of usage credits``/``extra usage`` and
``Your org is out of usage`` (its own classifier list); codex 0.155.1 has the
``You’ve hit your usage limit`` family (curly apostrophe; older builds used
U+0027), ``Your workspace is out of credits`` and ``You hit your spend cap``.
Warnings (``You've used 90% of…``, ``You're close to…``), the "now using
credits" notices and Claude's fast-mode cooldown (``You've hit your fast
limit``, after which it keeps working at normal speed) are deliberately left
out.  Gemini's wordings were not captured and are not guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "USAGE_LIMIT_PATTERNS",
    "USAGE_LIMIT_PEEK_LINES",
    "USAGE_LIMIT_TAIL_LINES",
    "UsageLimitScreen",
    "detect_usage_limit_screen",
    "match_usage_limit_screen",
]

#: Non-blank lines, counted up from the bottom of the pane, that may hold the
#: limit message.  Below it a CLI paints only its own chrome: a hint line, a
#: "worked for" line, the prompt box and a footer (8 lines for Claude 2.1.278).
USAGE_LIMIT_TAIL_LINES = 15

#: Pane lines to capture.  Blank padding rows are captured too, so this is
#: comfortably more than a screen.
USAGE_LIMIT_PEEK_LINES = 60

_APOS = "['\u2019]"
#: One clause of a limit name: stops at the ``·`` separator or a full stop so a
#: match cannot wander into the rest of the sentence.
_NAME = r"[^\n·.]{0,48}?"

#: Anchored at the start of the line, after :data:`_GUTTER`.  Matched
#: case-sensitively: these are the CLIs' own strings, not prose.
USAGE_LIMIT_PATTERNS: tuple[str, ...] = (
    # Claude: "limit", "session limit", "weekly limit", "Opus limit",
    # "usage limit", "monthly spend limit", "org's monthly usage limit",
    # "team's shared budget", ...  Codex: "usage limit." / "usage limit for
    # <model>."  Not "fast limit": fast mode's cooldown is not a stop.
    rf"You{_APOS}ve hit your (?!fast limit){_NAME}(?:limit|budget)\b",
    # Claude "Fable limit"; Codex "workspace credit limit", "usage limit".
    rf"You{_APOS}ve reached your {_NAME}limit\b",
    rf"You{_APOS}re out of (?:usage credits|extra usage|credits)\b",
    r"Your (?:org|organization|workspace) is out of (?:usage|credits)\b",
    # Codex: "You hit your spend cap set in your workspace."
    r"You hit your spend cap\b",
)

#: The CLI's own gutter in front of the message: Claude's ``⎿`` or Codex's
#: ``■`` glyph within three columns of the margin, then whitespace (``\s``
#: covers NBSP).  Both halves are required; see the module docstring.
_GUTTER = r"[ \t\u00a0]{0,3}[⎿■]\s*"

_SCREEN_RE = re.compile(rf"^{_GUTTER}(?:{'|'.join(USAGE_LIMIT_PATTERNS)})")

# OpenCode paints a spinner and its retry message below the composer, rather
# than a Claude/Codex gutter. Only callers that resolved the CLI may use this
# matcher. Require its retry status as well as an error prefix: quoting a
# limit string, a diff, a grep hit or text in the composer is not this screen.
# The status is always the end of the retry text (``<message>[ (click to
# expand)] [retrying [in <duration> ]attempt #N]``); the same terminal row
# then carries right-aligned chrome: the ``esc interrupt`` hint and, in
# 1.18.x, a ``• OpenCode <version>`` badge.  An anchor at the bracket's
# close missed the four workers parked on fresh-rapids-73's incident.
_OPENCODE_RE = re.compile(
    r"^[ \t\u00a0]{0,3}(?:(?:[▀-▟⠁-⣿⋯■⬝·]{1,16}|\[⋯\])[ \t]+)?"
    r"(?P<message>(?:Free limit reached|Free usage exceeded|"
    r"Usage limit reached|(?:Rate limit|Quota) (?:reached|exceeded)|"
    r"Too many requests|Internal server error|Bad gateway|Service unavailable)\b[^\n]*?)"
    r"\s+\[retrying (?:in (?P<duration>~?\d+\s*(?:days?|weeks?)\s*|(?:\d+\s*[dhms]\s*)+))?"
    r"(?:attempt #\d+)?(?P<status_end>\])(?:\s[^\n]*)?$",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(r"(\d+)\s*(days?|weeks?|[dhms])", re.IGNORECASE)
_DURATION_SECONDS = {
    "week": 604800, "weeks": 604800, "day": 86400, "days": 86400,
    "d": 86400, "h": 3600, "m": 60, "s": 1,
}
_FREE_EXHAUSTED_RE = re.compile(r"^Free (?:limit reached|usage exceeded)\b", re.IGNORECASE)


@dataclass(frozen=True)
class UsageLimitScreen:
    line: str
    retry_after: float | None = None
    # An explicit quota statement paired with the CLI's own retry status,
    # stronger than a broad rate-limit match or a long server retry alone.
    usage_exhausted: bool = False


def detect_usage_limit_screen(
    pane: str, *, opencode: bool = False, tail_lines: int = USAGE_LIMIT_TAIL_LINES
) -> UsageLimitScreen | None:
    """Read a blocking screen, including its reset countdown when available."""
    tail = [line for line in pane.splitlines() if line.strip()][-max(tail_lines, 1) :]
    for line in reversed(tail):
        if _SCREEN_RE.match(line) is not None:
            return UsageLimitScreen(line.lstrip(" \t\u00a0⎿■").rstrip())
        match = _OPENCODE_RE.match(line) if opencode else None
        if match is None:
            continue
        seconds = float(
            sum(
                int(value) * _DURATION_SECONDS[unit.lower()]
                for value, unit in _DURATION_RE.findall(match["duration"] or "")
            )
        )
        exhausted = _FREE_EXHAUSTED_RE.match(match["message"]) is not None
        if not exhausted and seconds < 3600:
            continue  # A normal brief server retry is still working.
        status = line[match.start("message") : match.end("status_end")]
        return UsageLimitScreen(status, seconds or None, exhausted)
    return None


def match_usage_limit_screen(
    pane: str, *, opencode: bool = False, tail_lines: int = USAGE_LIMIT_TAIL_LINES
) -> str | None:
    """Return the limit line if *pane* ends on a usage-limit screen, else ``None``.

    The returned line has its gutter stripped, so it can go straight into a
    verdict reason or a log line.
    """
    screen = detect_usage_limit_screen(pane, opencode=opencode, tail_lines=tail_lines)
    return screen.line if screen else None
