"""A file-driven script for the fake session provider (provider-failover D23).

The in-memory knobs on :class:`~src.sessions.fake.FakeProvider`
(``script_startup_dialog`` and friends) serve tests that hold the provider
object.  The end-to-end kit cannot: its daemon runs in another process.  So
``sessions.fake_script_file`` names a JSON file mapping a harness command to
a *mode*, and the fake provider re-reads it on every start -- a shell script
or the smoke runner exhausts and restores a provider mid-run by rewriting
one file::

    {"prova": "login_required", "provb": {"mode": "rate_limit_midtask", "after_s": 5}}

Modes:

``ok``
    The start succeeds (the default for a harness the file does not name).
``login_required``
    The start dies on the ``login-required`` quarantine dialog (``signal:
    auth``) -- a logged-out CLI, the 2026-09-20 incident.
``usage_limit``
    The start dies on the ``usage-limit`` dialog (``signal: usage``) -- an
    exhausted account.
``crash``
    The start dies with no dialog -- an unattributed startup death.
``rate_limit_midtask``
    The start succeeds; ``after_s`` seconds later (default 5) the process
    exits with a usage-limit line in its pane, which the exit classifier
    reads as ``RATE_LIMIT``.

The same file answers the daemon's login probe for the harnesses it names
(:func:`probe_answer`): ``login_required`` is ``not_authenticated``, any other
mode ``authenticated``.  A harness the file does not name gets no answer, so
a real CLI is never probed from a fake-provider daemon.

Test-only by construction: nothing reads the file unless ``sessions.provider``
is ``fake`` and the key is set.  A missing or unreadable file is an empty
script -- the kit's world, not a daemon fault.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "MODES",
    "ScriptedMode",
    "probe_answer",
    "read_script",
    "script_file_for",
]

MODES = ("ok", "login_required", "usage_limit", "crash", "rate_limit_midtask")

#: The pane line a ``rate_limit_midtask`` session dies on.  Worded like the
#: Codex CLI's own notice, and matched by ``RATE_LIMIT_PATTERNS``.
RATE_LIMIT_LINE = "■ You've hit your usage limit. Usage limit reached; try again in 2 hours."


@dataclass(frozen=True, slots=True)
class ScriptedMode:
    """One harness's scripted behaviour."""

    mode: str = "ok"
    after_s: float = 5.0


def script_file_for(config: Any) -> str:
    """The script path when this daemon runs fake sessions with one set, else ``""``."""
    sessions = getattr(config, "sessions", None)
    if sessions is None or getattr(sessions, "provider", "") != "fake":
        return ""
    return str(getattr(sessions, "fake_script_file", "") or "").strip()


def read_script(path: str) -> dict[str, ScriptedMode]:
    """Parse *path*; an absent, unreadable or malformed file is an empty script."""
    if not path:
        return {}
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("fake session script %s is unreadable; ignoring it", path, exc_info=True)
        return {}
    if not isinstance(raw, dict):
        logger.warning("fake session script %s is not a JSON object; ignoring it", path)
        return {}
    out: dict[str, ScriptedMode] = {}
    for harness, value in raw.items():
        if isinstance(value, str):
            entry = ScriptedMode(mode=value)
        elif isinstance(value, dict):
            try:
                after_s = float(value.get("after_s", 5.0))
            except (TypeError, ValueError):
                after_s = 5.0
            entry = ScriptedMode(mode=str(value.get("mode") or "ok"), after_s=after_s)
        else:
            continue
        if entry.mode not in MODES:
            logger.warning("fake session script: unknown mode %r for %s", entry.mode, harness)
            continue
        out[str(harness)] = entry
    return out


def probe_answer(path: str, provider: str) -> str | None:
    """The login probe's answer for *provider*, or ``None`` when the script is silent."""
    entry = read_script(path).get(provider)
    if entry is None:
        return None
    return "not_authenticated" if entry.mode == "login_required" else "authenticated"
