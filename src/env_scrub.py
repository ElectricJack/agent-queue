"""Environment scrubbing for agent subprocesses.

One pure function — :func:`scrub_env` — owns the policy described in
``docs/specs/design/trust-and-ops.md`` §3 (trust rule R6): a subprocess
launched on behalf of an agent starts from a *scrubbed* copy of the daemon
environment rather than the raw one.

The daemon's environment carries the messaging bot token, database DSNs,
embedding API keys and whatever else the operator's shell exports.  None of
that belongs in an agent's environment by default.

Scope, honestly
---------------
The scrub is applied where the daemon builds the child environment itself:
``src.sessions.env`` (every tmux session — i.e. every coding agent) and
``_cmd_run_command``.

The gap recorded in design §2.5 is closed: it described the old
``claude_sdk`` runtime, which merged ``ClaudeAgentOptions.env`` over a full
copy of ``os.environ`` with no way to *remove* an inherited key, so that
subprocess always saw the daemon environment.  That runtime was deleted in the
tmux-harness migration and the session path builds its environment explicitly.

Policy
------
A key is dropped when its name (upper-cased, ``-`` normalised to ``_``)
contains any of :data:`SENSITIVE_ENV_PATTERNS`, matches one of
:data:`SENSITIVE_ENV_REGEXES`, or carries a credential-bearing URI value —
unless it is exempt:

* :data:`BUILTIN_EXEMPT` — known false positives of the ``AUTH`` pattern.
* :data:`HARNESS_CREDENTIAL_ALLOWLIST` — the credentials an agent CLI needs
  in order to authenticate at all (applied unless ``harness_credentials`` is
  False).
* ``allowlist`` — operator-listed names or fnmatch globs
  (``security.env_allowlist`` in ``config.yaml``), matched case-insensitively.

The denylist is **best-effort, not complete** — see the note on
:data:`SENSITIVE_ENV_PATTERNS`.

:data:`STRIP_ALWAYS` keys are removed regardless of the sensitivity patterns
and regardless of the kill switch (they make a nested Claude CLI think it is
already inside a session).  An ``explicit`` entry for such a key still wins:
naming a key in a harness/profile ``env`` map is operator intent, and that
outranks an inherited value we are only guessing about.

``explicit`` entries — harness/profile ``env`` maps, ``AQ_*`` session markers,
the task-scoped ``AQ_API_TOKEN`` — are merged **last** and are never scrubbed.

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
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

# Substring patterns that mark a variable as sensitive.  Matched against the
# key upper-cased with ``-`` normalised to ``_`` (so ``API-KEY`` matches
# ``API_KEY``).
#
# NOTE (design §3): this is a **best-effort denylist**, not a complete one.  A
# substring list cannot enumerate every secret-shaped name an operator's shell
# might export, and this workstream argues elsewhere (§2.5, ``run_command``)
# that substring blocklists over *attacker-chosen* text are theater.  The
# difference here is the input: env var names come from the operator's own
# shell and the daemon's own config, not from an adversary trying to slip past
# the filter.  Against that input a denylist plus an explicit allowlist is the
# pragmatic control; against a hostile author it would not be.
SENSITIVE_ENV_PATTERNS: tuple[str, ...] = (
    "TOKEN",
    "API_KEY",
    "APIKEY",
    "SECRET",
    "PASSWORD",
    "PASSPHRASE",
    "CREDENTIAL",
    "PRIVATE",
    "AUTH",
    "DSN",
    "WEBHOOK",
    "NETRC",
    "KUBECONFIG",
)

# Anchored patterns for names a substring list would either miss or over-match.
# ``(^|_)KEY$`` catches ``SSH_KEY`` / ``SIGNING_KEY`` / ``OPENAI_KEY`` without
# dropping ``KEYBOARD_LAYOUT``; ``(^|_)PAT$`` catches ``GH_PAT`` without
# dropping ``LD_LIBRARY_PATH``.
SENSITIVE_ENV_REGEXES: tuple[str, ...] = (
    r"(?:^|_)KEY$",
    r"(?:^|_)PAT$",
    r"(?:^|_)ID_RSA(?:$|_)",
    r"(?:^|_)ID_ED25519(?:$|_)",
)

_SENSITIVE_RE = re.compile("|".join(SENSITIVE_ENV_REGEXES))

# A URI whose authority carries ``user:password@`` — the shape of a database
# DSN, an AMQP URL or a credentialed proxy.  Matched against the *value*, so
# ``DATABASE_URL=postgres://user:password@host/db`` is withheld even though
# nothing in the name says "secret".  Values are never logged or returned.
_CREDENTIALED_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/\s@]*:[^/\s@]*@")

# Known false positives of the AUTH pattern — always exempt.
BUILTIN_EXEMPT: tuple[str, ...] = (
    "GIT_AUTHOR_NAME",
    "GIT_AUTHOR_EMAIL",
    "GIT_AUTHOR_DATE",
)

# Credentials an agent harness legitimately needs in order to run at all.
#
# Design decision (trust-and-ops §3): the scrub ships **default-on** with this
# allowlist rather than default-off.  An agent CLI that cannot authenticate is
# not a safer agent, it is a broken install — and `ANTHROPIC_API_KEY` is the
# normal install shape (``src/setup_wizard.py`` writes it into the daemon env
# file).  Withholding the daemon's *own* secrets — bot token, database DSN,
# embedding keys, the operator's unrelated exports — is where the value is, and
# that survives intact.  Entries are fnmatch globs, matched case-insensitively.
#
# Vendor-prefix globs rather than a per-key list: agents reach many vendors
# and a new one's key name must not silently break it.  An operator who wants
# a harder lockdown turns the defaults off (``harness_credentials=False``) and
# names the exact keys in ``security.env_allowlist``.
HARNESS_CREDENTIAL_ALLOWLIST: tuple[str, ...] = (
    # Anthropic / Claude Code
    "ANTHROPIC_*",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    # OpenAI / Codex
    "OPENAI_*",
    "AZURE_OPENAI_*",
    "CODEX_API_KEY",
    # Google / Gemini
    "GEMINI_*",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "VERTEX_*",
    # Other ACP-compatible harness backends
    "OPENROUTER_*",
    "XAI_*",
    "MISTRAL_*",
    "GROQ_*",
    "DEEPSEEK_*",
    "TOGETHER_*",
    "PERPLEXITY_*",
    "CEREBRAS_*",
    "FIREWORKS_*",
    "QWEN_*",
    "ZAI_*",
    # Forge access — agents open PRs from inside their worktree
    "GH_TOKEN",
    "GITHUB_TOKEN",
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


def _normalize(key: str) -> str:
    return key.upper().replace("-", "_")


def _is_exempt(key: str, allowlist: Iterable[str]) -> bool:
    """Return True when *key* is explicitly allowed through the scrub."""
    upper = _normalize(key)
    if upper in BUILTIN_EXEMPT:
        return True
    for entry in allowlist:
        if not entry:
            continue
        pattern = _normalize(entry)
        if upper == pattern or fnmatch.fnmatchcase(upper, pattern):
            return True
    return False


def is_sensitive(key: str, value: str | None = None) -> bool:
    """Return True when *key* (or its *value*) looks secret-bearing.

    Ignores exemptions — :func:`scrub_env` applies those.  *value* is only
    inspected for the credentialed-URI shape (``scheme://user:pass@host``);
    it is never logged, returned, or included in any message.
    """
    upper = _normalize(key)
    if any(pattern in upper for pattern in SENSITIVE_ENV_PATTERNS):
        return True
    if _SENSITIVE_RE.search(upper):
        return True
    if value and _CREDENTIALED_URI_RE.match(value.strip()):
        return True
    return False


def scrub_env(
    base: Mapping[str, str] | None = None,
    *,
    allowlist: Iterable[str] = (),
    explicit: Mapping[str, str] | None = None,
    enabled: bool = True,
    harness_credentials: bool = True,
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
        harness_credentials: Apply :data:`HARNESS_CREDENTIAL_ALLOWLIST` on top
            of *allowlist*.  True for agent sessions (an agent CLI without its
            provider key cannot run).  Pass False for a child that has no
            business holding provider credentials — e.g. the daemon-host shell
            behind ``run_command``.

    Returns:
        A :class:`ScrubResult`.  ``dropped`` is sorted and contains key names
        only; ``os.environ`` and *base* are left untouched.
    """
    source: Mapping[str, str] = os.environ if base is None else base
    allow = tuple(allowlist or ())
    if harness_credentials:
        allow = allow + HARNESS_CREDENTIAL_ALLOWLIST

    env: dict[str, str] = {}
    dropped: list[str] = []

    for key, value in source.items():
        if key in STRIP_ALWAYS:
            dropped.append(key)
            continue
        if enabled and is_sensitive(key, value) and not _is_exempt(key, allow):
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
    harness_credentials: bool = True,
) -> ScrubResult:
    """Convenience wrapper reading the policy off an :class:`AppConfig`.

    Call sites that already hold ``self.config`` (command handler, runtimes)
    use this so the ``security`` section is read in exactly one way.  A config
    without a ``security`` section falls back to the shipped defaults.
    """
    security = getattr(config, "security", None)
    allowlist = getattr(security, "env_allowlist", ()) or ()
    enabled = bool(getattr(security, "env_scrub_enabled", True))
    return scrub_env(
        base,
        allowlist=allowlist,
        explicit=explicit,
        enabled=enabled,
        harness_credentials=harness_credentials,
    )
