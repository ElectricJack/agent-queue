"""Render one incident into the few messages Discord ever sees.

The channel post is the supervisor talking to a human on a phone: a leading
glyph, the decision that is needed, the context behind it, and one link to the
incident's page. §3.2 of the Discord design spec fixes that shape -- two body
lines and 400 characters for an open incident, one line and 160 once it is
closed, at most four lines and 600 characters inside the thread -- and
:mod:`src.discord.render_budget` cuts whatever a fact field carries down to
them, so no incident can post a wall of text. Everything that did not fit (the
investigation, the options, the history) waits in the thread, where there is
room for it.

This module is the whole of that surface, and it is pure -- the dispatcher
decides *when* to send, the renderer only decides what the text is. Three
safety rules are enforced here rather than trusted to callers:

* every interpolated string goes through :func:`sanitise`, so a task title, a
  supervisor note or a human reply that contains ``@everyone`` or a raw
  ``<@123>`` token cannot ping anybody;
* the configured mention is added by :meth:`MentionPolicy.render` alone, and
  only on the *initial* root post.  A resolution edit deliberately drops it.
  It sits directly after the glyph, the one position no truncation reaches;
* the link is built once, by the resolver and :func:`~src.dashboard_paths
  .escalation_path`, and never names a ``/settings/`` page.
"""

from __future__ import annotations

import re

from src.dashboard_paths import escalation_path
from src.discord.render_budget import (
    ESCALATION_CLOSED,
    ESCALATION_OPEN,
    ESCALATION_THREAD,
    GLYPH_LANDED,
    GLYPH_NEEDS_YOU,
    compose,
    cut,
    link_line,
)
from src.escalations.facts import EscalationFacts, MentionPolicy, TransportBinding

#: Longest a single interpolated field may be before it is elided. The post's
#: own budget is smaller (§3.2); this only keeps one field from dominating the
#: text that survives it.
MAX_FIELD_CHARS = 400
#: Longest a thread name may be.
MAX_THREAD_NAME_CHARS = 90

_MENTION_TOKEN = re.compile(r"<@[!&]?\d+>|<#\d+>")
_WHITESPACE = re.compile(r"[ \t]+")
_TRACEBACK = re.compile(r"traceback \(most recent call last\)", re.IGNORECASE)

#: Prefix of the operation marker embedded in every message this feature
#: sends.  It is what :meth:`~src.escalations.transport.EscalationTransport
#: .find_marker` looks for when a previous attempt ended ambiguously, so it
#: has to be stable and unique per delivery.
MARKER_PREFIX = "aq-esc"


def marker_for(dedup_key: str) -> str:
    """The operation marker for a delivery, embedded in the message text."""
    return f"{MARKER_PREFIX}:{dedup_key}"


def marker_line(dedup_key: str) -> str:
    """The marker as the small-print line it is rendered as.

    Not voice: it is delivery bookkeeping, and it is what lets a re-post or a
    post-after-crash recognise its own earlier attempt instead of announcing a
    second incident.
    """
    return f"-# {marker_for(dedup_key)}"


def sanitise(text: str, *, limit: int = MAX_FIELD_CHARS) -> str:
    """Make an arbitrary authored string safe to interpolate.

    Mention tokens are removed outright and a bare ``@`` keeps its character
    but gains a zero-width space, so no interpolated value can ping a user or
    a role.  Backticks are dropped so a value cannot break out of the
    surrounding formatting, and a pasted traceback is cut to its first line --
    escalation posts carry a decision, not a log dump.
    """
    text = _MENTION_TOKEN.sub("", text or "")
    if _TRACEBACK.search(text):
        text = text.split("\n", 1)[0]
    text = text.replace("`", "").replace("@", "@\u200b")
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > limit:
        text = cut(text, limit)
    return text


def _one_line(text: str, *, limit: int = MAX_FIELD_CHARS) -> str:
    return sanitise(text, limit=limit).replace("\n", " ")


def escalation_url(base_url: str, escalation_id: str, *, unavailable_notice: str = "") -> str:
    """The post's single deep link: the incident's page in the dashboard (§6.1).

    Never a ``/settings/`` page.  The old target was the messaging settings
    anchor, which a phone cannot answer from and which only named the machine
    that sent the post; the escalation page is where the same link is a place
    to reply, resolve and read the history.
    """
    return link_line(base_url, escalation_path(escalation_id), notice=unavailable_notice)


def thread_name(facts: EscalationFacts) -> str:
    """Name for the incident's one thread -- stable across replacements."""
    subject = _one_line(facts.task_title or facts.task_id or facts.summary, limit=60)
    name = f"{facts.project_id}: {subject}".strip()
    if len(name) > MAX_THREAD_NAME_CHARS:
        name = cut(name, MAX_THREAD_NAME_CHARS)
    return name or f"escalation {facts.id}"


def render_root(
    facts: EscalationFacts,
    *,
    mentions: MentionPolicy,
    base_url: str,
    dashboard_notice: str = "",
    dedup_key: str,
    replacement: bool = False,
) -> str:
    """The channel post: what is needed, why, and where to answer.

    §3.2 gives an open incident two body lines and 400 characters, and says the
    decision line is kept while the summary is what gets cut -- so the decision
    is line one and the summary line two, and :func:`~src.discord.render_budget
    .compose` overflows from the end.  The mention follows the glyph because
    that is the only position truncation never reaches.

    ``replacement=True`` re-posts a root whose original message was deleted.
    That is the same incident, not a new one, so the configured mention is
    deliberately dropped -- only the initial escalation is allowed to ping.
    """
    mention = "" if replacement else mentions.render()
    decision = _one_line(facts.decision_requested, limit=110)
    summary = _one_line(facts.summary, limit=120)
    headline = " ".join(
        part for part in (GLYPH_NEEDS_YOU, mention, _subject(facts)) if part
    )
    lines = [f"{headline} needs a decision: {decision}"]
    if replacement:
        lines[0] += " (reposted; the original was deleted)"
    lines.append("Reply here or on the page" + (f": {summary}" if summary else "."))
    return compose(
        lines,
        ESCALATION_OPEN,
        link=escalation_url(base_url, facts.id, unavailable_notice=dashboard_notice),
        marker=marker_line(dedup_key),
    )


def render_resolved_root(facts: EscalationFacts, *, dedup_key: str) -> str:
    """The root edited into its terminal state -- one line, no mention, no link.

    §3.2's collapsed row is a single line and 160 characters with no link, and
    §3.3's example shows none: how an incident ended is what a later reader of
    the channel needs, and the incident's page is already linked from the root
    above it and from the thread.  A real incident id is
    ``escalation-<uuid>``, so a link plus the delivery marker would spend the
    whole 160 characters and leave nothing to say.
    """
    verb = {"resolved": "Resolved", "cancelled": "Cancelled", "stale": "Stale"}.get(
        facts.state, facts.state.title()
    )
    outcome = _one_line(facts.terminal_outcome, limit=120) or _one_line(
        facts.decision_requested, limit=120
    )
    lines = [f"{GLYPH_LANDED} {verb}: {outcome}" if outcome else f"{GLYPH_LANDED} {verb}."]
    return compose(lines, ESCALATION_CLOSED, marker=marker_line(dedup_key))


def render_thread_opener(
    facts: EscalationFacts, *, base_url: str, dedup_key: str, dashboard_notice: str = ""
) -> str:
    """First message inside the thread: the context the channel post had no room for."""
    lines = [
        (
            "This thread is where this incident's follow-ups live. Anything written here goes "
            "to the project supervisor, which decides and performs the recovery."
        )
    ]
    if facts.investigation:
        lines.append(f"So far: {_one_line(facts.investigation, limit=300)}")
    if facts.choices:
        options = " · ".join(_one_line(choice, limit=80) for choice in facts.choices[:5])
        lines.append(f"Options: {options}")
    if facts.decision_requested:
        lines.append(f"Decision needed: {_one_line(facts.decision_requested, limit=200)}")
    return compose(
        lines,
        ESCALATION_THREAD,
        link=escalation_url(base_url, facts.id, unavailable_notice=dashboard_notice),
        marker=marker_line(dedup_key),
    )


def render_ack(facts: EscalationFacts, *, dedup_key: str) -> str:
    """Acknowledge one persisted human reply, once.

    A reply that arrived after the incident closed is still kept as history,
    but saying "the supervisor is reviewing it" would be a lie: no supervisor
    work was queued for it.  §7 asks for closed-state guidance instead, and
    the reply must not read as though it reopened anything.

    This one carries no link: it is a reply inside the incident's thread, where
    the root three messages above already named the page, and a second link in
    one incident is the repetition §3.1 forbids.
    """
    if facts.is_terminal:
        verb = {
            "resolved": "already resolved",
            "cancelled": "cancelled",
            "stale": "closed as stale",
        }.get(facts.state, f"closed ({facts.state})")
        headline = (
            f"🔒 This escalation was {verb} before your reply arrived, so it is "
            "recorded for the record but has not reopened the work. Raise a new "
            f"escalation in {facts.project_id} if a decision is still needed."
        )
    else:
        headline = (
            f"📥 Got it — your reply is recorded and the {facts.project_id} "
            "supervisor is reviewing it."
        )
    return compose([headline], ESCALATION_THREAD, marker=marker_line(dedup_key))


def render_relay(text: str, *, dedup_key: str) -> str:
    """Relay one correlated supervisor message into the incident's thread."""
    return compose(
        [f"🧭 Supervisor: {_one_line(text, limit=ESCALATION_THREAD.chars)}"],
        ESCALATION_THREAD,
        marker=marker_line(dedup_key),
    )


def render_resolution(
    facts: EscalationFacts, *, base_url: str, dedup_key: str, dashboard_notice: str = ""
) -> str:
    """The in-thread record of what was done, posted before the root is edited."""
    verb = {"resolved": "Resolved", "cancelled": "Cancelled", "stale": "Closed as stale"}.get(
        facts.state, facts.state.title()
    )
    lines = [f"{GLYPH_LANDED} {verb}."]
    if facts.terminal_outcome:
        lines.append(f"What was done: {_one_line(facts.terminal_outcome, limit=300)}")
    lines.append("This incident is closed; a later reply here will not reopen it.")
    return compose(
        lines,
        ESCALATION_THREAD,
        link=escalation_url(base_url, facts.id, unavailable_notice=dashboard_notice),
        marker=marker_line(dedup_key),
    )


def render_binding_note(binding: TransportBinding) -> str:
    """Short operator-facing description of where an incident is bound."""
    if not binding.has_root:
        return "not posted"
    thread = binding.thread_id or "—"
    return (
        f"channel {binding.channel_id} · message {binding.root_message_id} · "
        f"thread {thread} · generation {binding.generation}"
    )


def _subject(facts: EscalationFacts) -> str:
    """What the decision is about: the project, the task and its title.

    The escalation id is deliberately absent -- §3.1 says an id appears once,
    as the link, and repeating it here is how the post grew past its budget.
    """
    parts = [_one_line(facts.project_id, limit=30) or "the project"]
    title = _one_line(facts.task_title, limit=50)
    task_id = _one_line(facts.task_id, limit=40)
    if title and task_id:
        parts.append(f"{title} ({task_id})")
    else:
        parts.append(title or task_id)
    return " · ".join(part for part in parts if part)


__all__ = [
    "MARKER_PREFIX",
    "MAX_FIELD_CHARS",
    "MAX_THREAD_NAME_CHARS",
    "escalation_url",
    "marker_for",
    "marker_line",
    "render_ack",
    "render_binding_note",
    "render_relay",
    "render_resolution",
    "render_resolved_root",
    "render_root",
    "render_thread_opener",
    "sanitise",
    "thread_name",
]
