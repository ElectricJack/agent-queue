"""Environment scrubbing for agent subprocesses.

One pure function — :func:`scrub_env` — owns the policy described in
``docs/specs/design/trust-and-ops.md`` §3 (trust rule R6): every subprocess
launched on behalf of an agent starts from a *scrubbed* copy of the daemon
environment rather than the raw one.

The daemon's environment carries the messaging bot token, database DSNs,
embedding API keys and whatever else the operator's shell exports.  None of
that belongs in an agent's environment by default.

Policy
------
A key is dropped when its upper-cased name contains any of
:data:`SENSITIVE_ENV_PATTERNS`, unless it is exempt:

* :data:`BUILTIN_EXEMPT` — known false positives of the ``AUTH`` pattern.
* ``allowlist`` — operator-listed names or fnmatch globs
  (``security.env_allowlist`` in ``config.yaml``), matched case-insensitively.

:data:`STRIP_ALWAYS` keys are removed regardless of the patterns (they make a
nested Claude CLI think it is already inside a session).

``explicit`` entries — harness/profile ``env`` maps, ``AQ_*`` session markers,
the task-scoped ``AQ_API_TOKEN`` — are merged **last** and are never scrubbed:
setting a key explicitly is operator intent.

When ``enabled=False`` the scrub degrades to today's behavior (only
:data:`STRIP_ALWAYS` applies).  That is the ``security.env_scrub_enabled``
kill switch, not a separate code path.

The function performs no I/O, never mutates :data:`os.environ`, and returns
the names (never the values) of everything it dropped so ``aq doctor`` and
debug logs can show what was withheld.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

# Substring patterns (case-insensitive) that mark a variable as sensitive.
SENSITIVE_ENV_PATTERNS: tuple[str, ...] = (
    "TOKEN",
    "API_KEY",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "AUTH",
)

# Known false positives of the AUTH pattern — always exempt.
BUILTIN_EXEMPT: tuple[str, ...] = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
)

# Stripped regardless of the sensitivity patterns: these make the Claude CLI /
# SDK believe it is running inside an existing Claude Code session.
STRIP_ALWAYS: tuple[str, ...] = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


@dataclass
class ScrubResult:
    """Outcome of a scrub: the resulting env and an audit trail.

    ``dropped`` holds key **names only** — values never leave this module,
    so the result is safe to log or surface through ``aq doctor``.
    """

    env: dict[str, str]
    dropped: list[str] = field(default_factory=list)


def _is_exempt(key: str, allowlist: Iterable[str]) -> bool:
    """Return True when *key* is explicitly allowed through the scrub."""
    upper = key.upper()
    if upper in BUILTIN_EXEMPT:
        return True
    for entry in allowlist:
        if not entry:
            continue
        pattern = entry.upper()
        if upper == pattern or fnmatch.fnmatchcase(upper, pattern):
            return True
    return False


def is_sensitive(key: str) -> bool:
    """Return True when *key* matches a sensitivity pattern (ignoring exemptions)."""
    upper = key.upper()
    return any(pattern in upper for pattern in SENSITIVE_ENV_PATTERNS)


def scrub_env(
    base: Mapping[str, str] | None = None,
    *,
    allowlist: Iterable[str] = (),
    explicit: Mapping[str, str] | None = None,
    enabled: bool = True,
) -> ScrubResult:
    """Build a scrubbed environment for an agent subprocess.

    Args:
        base: Source environment.  Defaults to a snapshot of ``os.environ``.
        allowlist: Names or fnmatch globs that survive the scrub
            (``security.env_allowlist``).  Matched case-insensitively.
        explicit: Harness / profile / session values merged last.  These are
            never scrubbed — an operator who names a key means it.
        enabled: ``security.env_scrub_enabled``.  When False only
            :data:`STRIP_ALWAYS` is applied.

    Returns:
        A :class:`ScrubResult`.  ``dropped`` is sorted and contains key names
        only; ``os.environ`` and *base* are left untouched.
    """
    source: Mapping[str, str] = os.environ if base is None else base
    allow = tuple(allowlist or ())

    env: dict[str, str] = {}
    dropped: list[str] = []

    for key, value in source.items():
        if key in STRIP_ALWAYS:
            dropped.append(key)
            continue
        if enabled and is_sensitive(key) and not _is_exempt(key, allow):
            dropped.append(key)
            continue
        env[key] = value

    if explicit:
        # Explicit values always win, and re-introducing a key means it was
        # not withheld after all.
        explicit_keys = set(explicit)
        env.update(explicit)
        dropped = [k for k in dropped if k not in explicit_keys]

    return ScrubResult(env=env, dropped=sorted(dropped))


def scrub_env_from_config(
    config,
    *,
    base: Mapping[str, str] | None = None,
    explicit: Mapping[str, str] | None = None,
) -> ScrubResult:
    """Convenience wrapper reading the policy off an :class:`AppConfig`.

    Call sites that already hold ``self.config`` (command handler, runtimes)
    use this so the ``security`` section is read in exactly one way.  A config
    without a ``security`` section falls back to the shipped defaults.
    """
    security = getattr(config, "security", None)
    allowlist = getattr(security, "env_allowlist", ()) or ()
    enabled = bool(getattr(security, "env_scrub_enabled", True))
    return scrub_env(base, allowlist=allowlist, explicit=explicit, enabled=enabled)
