"""Per-harness hook output wrapping + suppression (design §5.4, §5.5).

Presentation-only: wrapping the already-rendered prime body for a specific
harness's hook contract. No I/O, no daemon calls — this is why ``aq prime
--hook-json`` can import this module directly instead of asking the server
to do the wrapping.
"""

from __future__ import annotations

import json
from typing import Mapping

# Env var session-runtime sets once the bootstrap argv prompt has already
# delivered the prime body to the agent (design §5.4). When set and a hook
# mode is requested, the hook body is suppressed so priming doesn't happen
# twice. Compact/resume SessionStart events bypass the marker in the CLI:
# compaction is exactly when restoring the continuation state pays for itself.
STARTUP_PROMPT_DELIVERED_ENV = "AQ_STARTUP_PROMPT_DELIVERED"

#: Harnesses whose SessionStart contract accepts structured JSON output
#: (design §5.5).  Knowledge delivery (``src/knowledge/delivery.py``) reads this
#: set rather than repeating the list, so "which harness has a hook envelope"
#: has exactly one answer in the tree.
HOOK_ENVELOPE_HARNESSES = frozenset({"claude", "codex"})


def wrap(body: str, harness: str) -> str:
    """Wrap *body* in the hook envelope for *harness*.

    ``"claude"`` / ``"codex"`` -> the ``SessionStart`` hook JSON envelope:
    ``{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": body}}``.

    Harnesses without a structured hook output format fall back to plain
    text (the body itself) — this is the documented default for any
    ``--hook-format <harness>`` not yet given a dedicated template (design
    §5.5: "harnesses without structured hook output -> plain text").
    """
    normalized = (harness or "").strip().lower()
    if normalized in HOOK_ENVELOPE_HARNESSES:
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": body,
                }
            }
        )
    return body


def suppressed(env: Mapping[str, str], hook_mode: bool, source: str | None = None) -> bool:
    """Return True when the hook body should be suppressed (design §5.4).

    Suppression fires when ``AQ_STARTUP_PROMPT_DELIVERED=1`` **and** a hook
    mode was requested — the bootstrap argv prompt already pointed the
    agent at ``.aq/prompt.md``, and double delivery would waste the exact
    tokens this design saves.
    """
    return (
        bool(hook_mode) and source not in {"resume", "compact"}
        and env.get(STARTUP_PROMPT_DELIVERED_ENV) == "1"
    )


# ---------------------------------------------------------------------------
# Subagent lifecycle hooks (SubagentStart / SubagentStop)
# ---------------------------------------------------------------------------

#: The two hook events that bound one native child agent's life.  Both
#: Claude Code (2.1.258) and Codex (0.151.0) name them identically, in
#: PascalCase, in the ``hook_event_name`` field of the stdin JSON.
SUBAGENT_EVENTS: Mapping[str, str] = {
    "subagentstart": "start",
    "subagentstop": "stop",
}


def parse_subagent_hook(raw: str) -> dict | None:
    """Normalize a ``SubagentStart`` / ``SubagentStop`` hook payload.

    Returns ``{"event", "subagent_id", "agent_type", "turn_id",
    "harness_session_id"}`` — or ``None`` when *raw* is not one of the two
    subagent events (malformed JSON, an empty stdin, or a different hook
    wired at the same command).  Returning ``None`` rather than raising is
    the point: this runs inside the agent's own process tree, and a hook
    that dies on an unexpected payload is a hook that stops sub-agents from
    starting.

    Both harnesses ship the same field names, verified live on 2026-09-01:

    * Claude Code — ``{session_id, transcript_path, cwd, hook_event_name,
      agent_id, agent_type}``; Stop adds ``stop_hook_active``,
      ``agent_transcript_path`` and ``last_assistant_message``.
    * Codex CLI — the same plus ``turn_id`` and ``model``; Stop likewise
      adds ``agent_transcript_path`` and ``last_assistant_message``.

    ``agent_id`` is the *child's* id on both halves, which is what makes a
    start pair exactly with its stop.  ``session_id`` here is the harness's
    own session id and is carried only as provenance — the daemon binds the
    event to the session that owns the bearer token, never to a value the
    payload claims.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    name = str(payload.get("hook_event_name") or "").strip().lower()
    event = SUBAGENT_EVENTS.get(name)
    if event is None:
        return None
    subagent_id = str(
        payload.get("agent_id") or payload.get("subagent_id") or ""
    ).strip()
    if not subagent_id:
        # A harness that stops sending an id would otherwise collapse every
        # child onto one row and read as "1 sub-agent, forever".  Fall back
        # to the child's own transcript, which is per-invocation on both
        # harnesses, before giving up on pairing entirely.
        subagent_id = str(payload.get("agent_transcript_path") or "").strip()
    return {
        "event": event,
        "subagent_id": subagent_id,
        "agent_type": str(payload.get("agent_type") or "").strip() or None,
        "turn_id": str(payload.get("turn_id") or "").strip() or None,
        "harness_session_id": str(payload.get("session_id") or "").strip() or None,
    }
