"""Keep secrets out of the installer's durable record and its output.

The contract is blunt: the resume record "contains no provider credential, raw
DSN password, OAuth/device code, shell history, project path, or task data",
and a login step must never claim to have handled a credential.  Two mechanisms
implement that here:

* :func:`redact` rewrites a payload, replacing any secret-shaped value with
  ``"[redacted]"`` — the mechanism a step uses when it wants to report *that*
  a value exists without reporting the value.
* :func:`assert_secret_free` is the fence.  The state writer runs it over the
  whole record before it touches the disk, so a step that forgets to redact
  fails loudly at write time instead of leaving a token in
  ``~/.agent-queue/install-state.json``.

Key sensitivity is delegated to :func:`src.env_scrub.is_sensitive`, which
already owns the project's denylist (``TOKEN``/``API_KEY``/``SECRET``/``DSN``
substrings, anchored ``…_KEY``/``…_PAT`` names, and credential-bearing URI
values).  There is deliberately one denylist in the tree, not two.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from src.env_scrub import is_sensitive

REDACTED = "[redacted]"

#: Value shapes that are secret regardless of the key they sit under: a
#: credentialed URI, an OAuth/device code, and the common provider key
#: prefixes.  Steps report identifiers, not material, so this is a safety net
#: for a hand-written summary rather than the primary control.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/\s@]*:[^/\s@]*@"),
    re.compile(r"^sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"^ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"^gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"^(?:ya29|1//)[A-Za-z0-9._\-]{10,}"),
    re.compile(r"^-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def is_secret_value(value: Any) -> bool:
    """True when *value* looks like credential material on its own."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    return any(pattern.match(candidate) for pattern in _VALUE_PATTERNS)


#: Names the ``AUTH``/``KEY`` substrings in the shared denylist would flag,
#: but which the installer legitimately reports.  Every one of them carries a
#: state or a method name ("browser", "api-key", ``True``), never material.
#: Keeping this list short and explicit is the point: an unlisted secret-shaped
#: name stays flagged.
SAFE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "authenticated",
        "authentication",
        "auth_method",
        "auth_methods",
        "auth_state",
        "authorized",
        "credential_source",
        "credential_store",
        "keychain",
        "keyring",
    }
)


def is_secret(key: str | None, value: Any) -> bool:
    """True when *key* names a secret, or *value* is secret-shaped.

    Only strings can be credential material: a bool or an int under a
    secret-shaped name (``{"authenticated": true}``) is a fact about a
    credential, not the credential, and flagging it would push steps into
    inventing euphemisms for ordinary fields.
    """
    if is_secret_value(value):
        return True
    if key is None or not isinstance(value, str) or not value.strip():
        return False
    if key.strip().lower().replace("-", "_") in SAFE_KEY_NAMES:
        return False
    return is_sensitive(key, value)


def redact(payload: Any, *, key: str | None = None) -> Any:
    """Return *payload* with every secret-shaped leaf replaced by ``[redacted]``.

    Containers are rebuilt rather than mutated, so a caller's own dict is never
    changed underneath it.  A secret-named key keeps its *name* — knowing that
    a step recorded ``api_key`` is useful; the value is not.
    """
    if isinstance(payload, Mapping):
        return {str(k): redact(v, key=str(k)) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)) or (
        isinstance(payload, Sequence) and not isinstance(payload, (str, bytes))
    ):
        return [redact(item, key=key) for item in payload]
    if is_secret(key, payload):
        return REDACTED
    return payload


def find_secrets(payload: Any, *, key: str | None = None, path: str = "") -> list[str]:
    """Return the dotted paths of every unredacted secret in *payload*."""
    found: list[str] = []
    if isinstance(payload, Mapping):
        for k, value in payload.items():
            child = f"{path}.{k}" if path else str(k)
            found.extend(find_secrets(value, key=str(k), path=child))
        return found
    if isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found.extend(find_secrets(item, key=key, path=f"{path}[{index}]"))
        return found
    if payload is None or payload == REDACTED:
        return found
    if is_secret(key, payload):
        found.append(path or (key or "<root>"))
    return found


class SecretLeakError(RuntimeError):
    """Raised when a payload that must be secret-free is not."""

    def __init__(self, paths: Sequence[str], *, context: str) -> None:
        self.paths = tuple(paths)
        super().__init__(
            f"refusing to persist {context}: secret-shaped value(s) at " + ", ".join(self.paths)
        )


def assert_secret_free(payload: Any, *, context: str) -> None:
    """Raise :class:`SecretLeakError` when *payload* carries a secret."""
    leaks = find_secrets(payload)
    if leaks:
        raise SecretLeakError(leaks, context=context)
