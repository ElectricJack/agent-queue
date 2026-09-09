"""Turn an eligible window into one short, safe message.

Constraints from §8, all enforced here rather than by the transport:

* one message, ``<=1200`` characters -- never split into several posts;
* counts first, then at most three highlights grouped by project, then an
  aggregate overflow count and the dashboard link;
* plain language: one line per highlight, no stack traces or raw logs;
* no mentions, ever, including text a human or agent wrote that merely looks
  like one.  A routine digest that pings a role is exactly the noise this
  redesign removes, so the escape is applied to every interpolated string
  rather than trusted to the callers.
"""

from __future__ import annotations

import re

from src.digest.eligibility import Eligibility
from src.digest.facts import (
    KIND_COMPLETED,
    KIND_PROGRESS,
    KIND_STARTED,
    DigestWindow,
)

#: Hard ceiling for the rendered message.
MAX_CHARS = 1200

#: How many highlights a normal digest shows before it switches to a count.
MAX_HIGHLIGHTS = 3

#: Longest a single highlight line may be before it is elided.
MAX_HIGHLIGHT_CHARS = 120

_MENTION_TOKEN = re.compile(r"<@[!&]?\d+>|<#\d+>")
_WHITESPACE = re.compile(r"\s+")
_TRACEBACK = re.compile(r"traceback \(most recent call last\)", re.IGNORECASE)

_KIND_VERB = {
    KIND_COMPLETED: "completed",
    KIND_PROGRESS: "progress",
    KIND_STARTED: "started",
}


def sanitise(text: str) -> str:
    """One safe line: no mentions, no newlines, no log spill.

    Raw Discord mention tokens are dropped outright; a bare ``@`` keeps its
    character but gains a zero-width space so ``@everyone`` typed by a human
    into a task title cannot ping a server from a routine digest.
    """
    text = _MENTION_TOKEN.sub("", text or "")
    if _TRACEBACK.search(text):
        # A pasted traceback is never a useful highlight; keep the first line.
        text = text.split("\n", 1)[0]
    text = _WHITESPACE.sub(" ", text).strip()
    text = text.replace("`", "")
    return text.replace("@", "@\u200b")


def _elide(text: str, limit: int = MAX_HIGHLIGHT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _window_label(window: DigestWindow) -> str:
    hours = window.hours
    if window.catchup:
        return f"catch-up, last {hours:.0f}h"
    if hours <= 0:
        return "latest"
    if abs(hours - 1.0) < 0.01:
        return "last hour"
    return f"last {hours:.0f}h"


def _highlight_line(fact, project_names: dict[str, str]) -> str:
    project = sanitise(project_names.get(fact.project_id, fact.project_id))
    detail = sanitise(fact.detail) or sanitise(fact.title)
    verb = _KIND_VERB.get(fact.kind, fact.kind)
    return _elide(f"• {project}: {verb} — {detail} ({sanitise(fact.task_id)})")


def render_digest(
    eligibility: Eligibility,
    window: DigestWindow,
    *,
    project_names: dict[str, str] | None = None,
    dashboard_url: str = "",
    open_escalations: int = 0,
    max_chars: int = MAX_CHARS,
    max_highlights: int = MAX_HIGHLIGHTS,
) -> str:
    """Render the message for an eligible window.

    Raises ``ValueError`` if asked to render a suppressed window: silence has
    no text, and a caller that renders one anyway is a bug worth failing on.
    """
    if not eligibility.send:
        raise ValueError(f"suppressed window has no message ({eligibility.reason})")

    names = project_names or {}
    header = f"**Agent Queue — {_window_label(window)}**"

    completed = eligibility.completed_count
    started = sum(1 for fact in eligibility.facts if fact.kind == KIND_STARTED)
    progressed = sum(1 for fact in eligibility.facts if fact.kind == KIND_PROGRESS)
    counts = []
    if completed:
        counts.append(f"{completed} completed")
    if progressed:
        counts.append(f"{progressed} progressed")
    if started:
        counts.append(f"{started} started")
    counts.append(f"{eligibility.active_count} active")
    summary = " · ".join(counts)

    # Highlights are already ordered most-informative-first by eligibility;
    # grouping by project keeps one project's lines together without
    # reordering the ranking between groups.
    ordered = list(eligibility.facts)
    ordered.sort(key=lambda f: (f.rank, names.get(f.project_id, f.project_id), -f.at, f.key))

    shown = ordered[: max(0, max_highlights)]
    footer_parts = []
    overflow = len(ordered) - len(shown)
    if overflow > 0:
        footer_parts.append(f"+{overflow} more")
    if open_escalations > 0:
        word = "escalation" if open_escalations == 1 else "escalations"
        footer_parts.append(f"{open_escalations} open {word}")
    if dashboard_url:
        footer_parts.append(sanitise(dashboard_url))
    footer = " · ".join(footer_parts)

    def assemble(lines: list[str]) -> str:
        body = [header, summary, *lines]
        if footer:
            body.append(footer)
        return "\n".join(part for part in body if part)

    lines = [_highlight_line(fact, names) for fact in shown]
    text = assemble(lines)
    while lines and len(text) > max_chars:
        # Drop the least informative highlight and fold it into the count
        # rather than splitting the digest across messages.
        lines.pop()
        overflow += 1
        footer_parts = [p for p in footer_parts if not p.startswith("+")]
        footer_parts.insert(0, f"+{overflow} more")
        footer = " · ".join(footer_parts)
        text = assemble(lines)
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 1)].rstrip() + "…"
    return text
