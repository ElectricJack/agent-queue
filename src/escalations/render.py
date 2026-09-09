"""Render one incident into the few messages Discord ever sees.

§7 fixes the shape of each of them: the channel post names the project, the
task, the blocker, the exact decision needed, the configured mention, a
dashboard link and the escalation ID; everything after that stays in the
thread.  This module is the whole of that surface, and it is pure — the
dispatcher decides *when* to send, the renderer only decides what the text is.

Two safety rules are enforced here rather than trusted to callers:

* every interpolated string goes through :func:`sanitise`, so a task title, a
  supervisor note or a human reply that contains ``@everyone`` or a raw
  ``<@123>`` token cannot ping anybody;
* the configured mention is added by :meth:`MentionPolicy.render` alone, and
  only on the *initial* root post.  A resolution edit deliberately drops it.
"""

from __future__ import annotations

import re

from src.escalations.facts import EscalationFacts, MentionPolicy, TransportBinding

#: Discord's hard per-message ceiling is 2000; leave room for the marker line.
MAX_CHARS = 1900
#: Longest a single interpolated field may be before it is elided.
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


def sanitise(text: str, *, limit: int = MAX_FIELD_CHARS) -> str:
    """Make an arbitrary authored string safe to interpolate.

    Mention tokens are removed outright and a bare ``@`` keeps its character
    but gains a zero-width space, so no interpolated value can ping a user or
    a role.  Backticks are dropped so a value cannot break out of the
    surrounding formatting, and a pasted traceback is cut to its first line —
    escalation posts carry a decision, not a log dump.
    """
    text = _MENTION_TOKEN.sub("", text or "")
    if _TRACEBACK.search(text):
        text = text.split("\n", 1)[0]
    text = text.replace("`", "").replace("@", "@\u200b")
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _one_line(text: str, *, limit: int = MAX_FIELD_CHARS) -> str:
    return sanitise(text, limit=limit).replace("\n", " ")


def _clamp(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[: MAX_CHARS - 1].rstrip() + "…"


def _task_line(facts: EscalationFacts) -> str:
    if not facts.task_id:
        return "**Task:** —"
    title = _one_line(facts.task_title or facts.task_id, limit=180)
    status = _one_line(facts.task_status or "", limit=32)
    suffix = f" · {status}" if status else ""
    return f"**Task:** {title} (`{_one_line(facts.task_id, limit=64)}`){suffix}"


def escalation_url(base_url: str, escalation_id: str) -> str:
    """Deep link to the dashboard escalation inbox entry."""
    base = (base_url or "").rstrip("/")
    return f"{base}/settings/messaging#escalation-reply-{escalation_id}"


def thread_name(facts: EscalationFacts) -> str:
    """Name for the incident's one thread — stable across replacements."""
    subject = _one_line(facts.task_title or facts.task_id or facts.summary, limit=60)
    name = f"{facts.project_id}: {subject}".strip()
    if len(name) > MAX_THREAD_NAME_CHARS:
        name = name[: MAX_THREAD_NAME_CHARS - 1].rstrip() + "…"
    return name or f"escalation {facts.id}"


def render_root(
    facts: EscalationFacts,
    *,
    mentions: MentionPolicy,
    base_url: str,
    dedup_key: str,
    replacement: bool = False,
) -> str:
    """The channel post: project, task, blocker, decision, mention, link, ID.

    ``replacement=True`` re-posts a root whose original message was deleted.
    That is the same incident, not a new one, so the configured mention is
    deliberately dropped — only the initial escalation is allowed to ping.
    """
    mention = "" if replacement else mentions.render()
    header = f"{facts.severity_label} · **Human decision needed** · `{facts.project_id}`"
    if replacement:
        header += " *(reposted — original message was deleted)*"
    lines = [f"{header} {mention}".rstrip(), _task_line(facts)]
    lines.append(f"**Blocker:** {_one_line(facts.summary)}")
    if facts.investigation:
        lines.append(f"**Already tried:** {_one_line(facts.investigation)}")
    lines.append(f"**Decision needed:** {_one_line(facts.decision_requested)}")
    if facts.choices:
        rendered = "  ".join(
            f"{index}) {_one_line(choice, limit=80)}"
            for index, choice in enumerate(facts.choices[:5], start=1)
        )
        lines.append(f"**Options:** {rendered}")
    lines.append(
        "Reply in the thread below — the project supervisor reads it and decides. "
        f"Details: {escalation_url(base_url, facts.id)}"
    )
    lines.append(f"-# escalation `{facts.id}` · {marker_for(dedup_key)}")
    return _clamp("\n".join(lines))


def render_resolved_root(
    facts: EscalationFacts,
    *,
    base_url: str,
    dedup_key: str,
) -> str:
    """The root edited into its terminal state — deliberately mention-free."""
    verb = {"resolved": "Resolved", "cancelled": "Cancelled", "stale": "Stale"}.get(
        facts.state, facts.state.title()
    )
    lines = [
        f"✅ **{verb}** · `{facts.project_id}`",
        _task_line(facts),
        f"**Decision needed was:** {_one_line(facts.decision_requested)}",
    ]
    if facts.terminal_outcome:
        lines.append(f"**Outcome:** {_one_line(facts.terminal_outcome)}")
    lines.append(f"No reply is needed here. Details: {escalation_url(base_url, facts.id)}")
    lines.append(f"-# escalation `{facts.id}` · {marker_for(dedup_key)}")
    return _clamp("\n".join(lines))


def render_thread_opener(facts: EscalationFacts, *, base_url: str, dedup_key: str) -> str:
    """First message inside the thread: context, not a second copy of the post."""
    lines = [
        (
            "This thread is where this incident's follow-ups live. "
            "Anything you write here goes to the project supervisor, which decides "
            "and performs the recovery — it is not typed into a worker."
        ),
    ]
    if facts.investigation:
        lines.append(f"**Investigation so far:** {sanitise(facts.investigation, limit=900)}")
    lines.append(f"**Decision needed:** {_one_line(facts.decision_requested)}")
    lines.append(f"Dashboard: {escalation_url(base_url, facts.id)}")
    lines.append(f"-# {marker_for(dedup_key)}")
    return _clamp("\n".join(lines))


def render_ack(facts: EscalationFacts, *, dedup_key: str) -> str:
    """Acknowledge one persisted human reply, once.

    A reply that arrived after the incident closed is still kept as history,
    but saying "the supervisor is reviewing it" would be a lie: no supervisor
    work was queued for it.  §7 asks for closed-state guidance instead, and
    the reply must not read as though it reopened anything.
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
            f"escalation in `{facts.project_id}` if a decision is still needed."
        )
    else:
        headline = (
            f"📥 Got it — your reply is recorded and the `{facts.project_id}` "
            "supervisor is reviewing it."
        )
    return _clamp("\n".join([headline, f"-# {marker_for(dedup_key)}"]))


def render_relay(text: str, *, dedup_key: str) -> str:
    """Relay one correlated supervisor message into the incident's thread."""
    return _clamp(
        "\n".join(
            [f"🧭 **Supervisor:** {sanitise(text, limit=1500)}", f"-# {marker_for(dedup_key)}"]
        )
    )


def render_resolution(facts: EscalationFacts, *, base_url: str, dedup_key: str) -> str:
    """The in-thread record of what was done, posted before the root is edited."""
    verb = {"resolved": "Resolved", "cancelled": "Cancelled", "stale": "Closed as stale"}.get(
        facts.state, facts.state.title()
    )
    lines = [f"✅ **{verb}.**"]
    if facts.terminal_outcome:
        lines.append(f"**Action and outcome:** {sanitise(facts.terminal_outcome, limit=1200)}")
    lines.append(
        "This incident is closed; a later reply here will not reopen it. "
        f"Details: {escalation_url(base_url, facts.id)}"
    )
    lines.append(f"-# {marker_for(dedup_key)}")
    return _clamp("\n".join(lines))


def render_binding_note(binding: TransportBinding) -> str:
    """Short operator-facing description of where an incident is bound."""
    if not binding.has_root:
        return "not posted"
    thread = binding.thread_id or "—"
    return (
        f"channel {binding.channel_id} · message {binding.root_message_id} · "
        f"thread {thread} · generation {binding.generation}"
    )
