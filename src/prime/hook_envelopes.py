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
# twice. Post-compaction SessionStart events *do* re-prime — session-runtime
# clears this variable's effect there by design (compaction is exactly when
# re-priming pays for itself). This module only reads the env var; setting
# and clearing it is session-runtime's responsibility.
STARTUP_PROMPT_DELIVERED_ENV = "AQ_STARTUP_PROMPT_DELIVERED"


def wrap(body: str, harness: str) -> str:
    """Wrap *body* in the hook envelope for *harness*.

    ``"claude"`` -> the Claude Code ``SessionStart`` hook JSON envelope
    (design §5.4):
    ``{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": body}}``.

    Harnesses without a structured hook output format fall back to plain
    text (the body itself) — this is the documented default for any
    ``--hook-format <harness>`` not yet given a dedicated template (design
    §5.5: "harnesses without structured hook output -> plain text").
    """
    normalized = (harness or "").strip().lower()
    if normalized == "claude":
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": body,
                }
            }
        )
    return body


def suppressed(env: Mapping[str, str], hook_mode: bool) -> bool:
    """Return True when the hook body should be suppressed (design §5.4).

    Suppression fires when ``AQ_STARTUP_PROMPT_DELIVERED=1`` **and** a hook
    mode was requested — the bootstrap argv prompt already pointed the
    agent at ``.aq/prompt.md``, and double delivery would waste the exact
    tokens this design saves.
    """
    return bool(hook_mode) and env.get(STARTUP_PROMPT_DELIVERED_ENV) == "1"
