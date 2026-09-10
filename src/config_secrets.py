"""Literal-secret redaction for the raw YAML config surface.

:func:`src.config_editor.read_raw_config` deliberately returns the config as
written on disk so ``${ENV_VAR}`` placeholders survive a round trip through the
editor UI.  It says nothing about credentials an operator wrote *literally* —
``database.url: postgresql://aq:hunter2@db/aq``, ``discord.bot_token: xoxb-…``
— and those literals reach every reader of the Config API: the dashboard,
``aq system config get``, the MCP mirror, and any screenshot of either.

This module is the read-side redactor plus the write-side restorer that keeps
redaction from destroying what it hides (``update_config`` replaces a whole
top-level section, so a placeholder that came back unchanged would otherwise be
written over the stored credential):

* :func:`redact_config` replaces literal credentials with
  :data:`SECRET_PLACEHOLDER`, leaves ``${ENV_VAR}`` references and non-secret
  structure byte-identical, and reports every dotted path it touched.
* :func:`restore_section_secrets` is the inverse for the write path: a section
  coming back from a client with placeholders still in it gets the stored
  literals spliced back in.  A placeholder with no stored counterpart is *not*
  guessed — it is reported as unresolved so the caller can refuse the write.

A client that means to change a credential sends the new literal value; only an
exact placeholder (or a DSN whose password is the placeholder) is ever
restored, so a deliberate rotation is never swallowed.

Design: ``docs/superpowers/specs/2026-09-10-config-api-secret-redaction-design.md``.
"""

from __future__ import annotations

import re
from typing import Any

#: What a redacted value reads as.  Deliberately grep-friendly and
#: implausible as a real setting, so :func:`restore_section_secrets` can match
#: it exactly rather than heuristically.
SECRET_PLACEHOLDER = "__aq_redacted__"

_ENV_REF = re.compile(r"\$\{\w+\}")

#: Keys whose *string* value is treated as a credential.
_SECRET_KEY = re.compile(
    r"(api[_-]?key"
    r"|access[_-]?key"
    r"|secret"
    r"|password|passwd"
    r"|credential"
    r"|private[_-]?key"
    r"|auth[_-]?token"
    r"|bearer"
    r"|(?:^|[_-])tokens?(?:[_-]|$)"
    r"|(?:^|[_-])dsn(?:[_-]|$)"
    r"|session[_-]?key)",
    re.IGNORECASE,
)

#: Suffixes that make a secret-shaped key demonstrably not a secret: a path to
#: a key file, a duration, a budget, a list of user ids.  Checked *after*
#: :data:`_SECRET_KEY` matches, so ``private_key_path`` and
#: ``authorized_users`` stay readable while ``private_key`` does not.
_NON_SECRET_KEY = re.compile(
    r"(_path|_paths|_file|_files|_dir|_dirs"
    r"|_seconds|_minutes|_hours|_days|_ttl|_ttl_hours"
    r"|_daily|_budget|_window|_ceiling|_limit|_count|_max|_min"
    r"|_enabled|_id|_ids|_users|_name|_names|_var|_vars|_env)$",
    re.IGNORECASE,
)

#: A plural ``…_tokens`` key that counts tokens rather than holding one:
#: ``llm.max_tokens``, ``max_daily_playbook_tokens``, ``context_max_tokens``.
#: A credential list (``auth_tokens``, ``api_keys``) carries no quantity word
#: and is still treated as secret.
_TOKEN_QUANTITY_KEY = re.compile(
    r"(?:^|[_-])(?:max|min|total|num|count|avg|used|remaining|budget|limit"
    r"|window|daily|weekly|monthly|per)(?:[_-]|$)",
    re.IGNORECASE,
)

#: Provider token shapes that are credentials whatever key they sit under.
_SECRET_VALUE = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{12,}"
    r"|sk-[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9_]{12,}"
    r"|github_pat_[A-Za-z0-9_]{12,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)

#: ``scheme://user:password@host`` — the password group is what gets replaced,
#: so the scheme, user, host, port and database name stay visible.
_URL_CREDENTIALS = re.compile(
    r"(?P<prefix>[A-Za-z][A-Za-z0-9+.\-]*://[^\s:/?#@]*:)(?P<password>[^\s@/?#]+)(?P<suffix>@)"
)


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


def is_secret_key(key: str) -> bool:
    """True when *key* names a credential rather than a knob about one."""
    if not key:
        return False
    if _NON_SECRET_KEY.search(key):
        return False
    if key.lower().endswith("tokens") and _TOKEN_QUANTITY_KEY.search(key):
        return False
    return bool(_SECRET_KEY.search(key))


def redact_config(raw: Any) -> tuple[Any, list[str]]:
    """Return ``(redacted, paths)`` for a raw config document.

    *raw* is never mutated.  ``paths`` holds every dotted path whose value was
    replaced or partially replaced, in document order — ``database.url``,
    ``discord.bot_token``, ``llm.providers[0].api_key``.
    """
    paths: list[str] = []
    redacted = _redact(raw, path="", key="", paths=paths)
    return redacted, paths


def _redact(value: Any, *, path: str, key: str, paths: list[str]) -> Any:
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for child_key, child in value.items():
            name = str(child_key)
            child_path = f"{path}.{name}" if path else name
            out[child_key] = _redact(child, path=child_path, key=name, paths=paths)
        return out
    if isinstance(value, list):
        return [
            # List items inherit the parent key: a list under ``api_keys:`` is
            # a list of credentials.
            _redact(item, path=f"{path}[{index}]", key=key, paths=paths)
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        return _redact_string(value, path=path, key=key, paths=paths)
    return value


def _redact_string(text: str, *, path: str, key: str, paths: list[str]) -> str:
    if not text.strip():
        return text
    has_env_ref = bool(_ENV_REF.search(text))

    if is_secret_key(key) and not has_env_ref:
        paths.append(path)
        return SECRET_PLACEHOLDER

    if _SECRET_VALUE.search(text):
        paths.append(path)
        return SECRET_PLACEHOLDER

    match = _URL_CREDENTIALS.search(text)
    if match and not _ENV_REF.search(match.group("password")):
        paths.append(path)
        return text[: match.start("password")] + SECRET_PLACEHOLDER + text[match.end("password") :]

    return text


# ---------------------------------------------------------------------------
# Write side
# ---------------------------------------------------------------------------


def restore_section_secrets(incoming: Any, stored: Any) -> tuple[Any, list[str]]:
    """Splice stored credentials back into a section a client sent back.

    *incoming* is what the client wants written; *stored* is the same section as
    it currently reads on disk (unredacted), or ``None`` when the section is
    new.  Returns ``(restored, unresolved)``:

    * a value that is exactly :data:`SECRET_PLACEHOLDER` becomes the stored
      string at the same path;
    * a DSN whose password is the placeholder gets the stored DSN's password,
      keeping every other edit the client made to that URL;
    * everything else passes through verbatim — that is how a deliberate
      credential change is written.

    ``unresolved`` lists the dotted paths where a placeholder survived because
    there was nothing stored to restore (a renamed key, a new field, a
    hand-pasted placeholder).  Callers must refuse the write when it is
    non-empty rather than persist the sentinel.
    """
    if not _mentions_placeholder(incoming):
        return incoming, []
    unresolved: list[str] = []
    restored = _restore(incoming, stored, path="", unresolved=unresolved)
    return restored, unresolved


def _mentions_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return SECRET_PLACEHOLDER in value
    if isinstance(value, dict):
        return any(_mentions_placeholder(child) for child in value.values())
    if isinstance(value, list):
        return any(_mentions_placeholder(item) for item in value)
    return False


def _restore(incoming: Any, stored: Any, *, path: str, unresolved: list[str]) -> Any:
    if isinstance(incoming, dict):
        out: dict[Any, Any] = {}
        for key, child in incoming.items():
            name = str(key)
            child_path = f"{path}.{name}" if path else name
            child_stored = stored.get(key) if isinstance(stored, dict) else None
            out[key] = _restore(child, child_stored, path=child_path, unresolved=unresolved)
        return out
    if isinstance(incoming, list):
        out_list: list[Any] = []
        for index, item in enumerate(incoming):
            child_stored = (
                stored[index] if isinstance(stored, list) and index < len(stored) else None
            )
            out_list.append(
                _restore(item, child_stored, path=f"{path}[{index}]", unresolved=unresolved)
            )
        return out_list
    if isinstance(incoming, str) and SECRET_PLACEHOLDER in incoming:
        return _restore_string(incoming, stored, path=path, unresolved=unresolved)
    return incoming


def _restore_string(incoming: str, stored: Any, *, path: str, unresolved: list[str]) -> str:
    if incoming == SECRET_PLACEHOLDER:
        if isinstance(stored, str) and stored:
            return stored
        unresolved.append(path)
        return incoming

    match = _URL_CREDENTIALS.search(incoming)
    if match and match.group("password") == SECRET_PLACEHOLDER and isinstance(stored, str):
        stored_match = _URL_CREDENTIALS.search(stored)
        if stored_match:
            return (
                incoming[: match.start("password")]
                + stored_match.group("password")
                + incoming[match.end("password") :]
            )

    # A placeholder somewhere we cannot account for: never write the sentinel.
    unresolved.append(path)
    return incoming
