"""Provider intent: did anyone mean the provider a task's profile names?

``docs/specs/provider-failover.md`` D8-D9.  ``profile_id`` says both "run
this class" and "run on this provider"; ``tasks.provider_intent`` answers the
one question it cannot:

* ``pinned`` -- a human chose this provider on purpose.  Held, never moved
  automatically, while its provider is unavailable.
* ``preferred`` -- somebody named a provider as a preference.  This is what an
  explicit ``profile_id`` means by default.  Fails over (D12).
* ``class_only`` -- routing placed it; nobody chose the provider.  Fails over,
  and its placement does not narrow the routing catalog (D8).

Pure: no I/O, no clock.
"""

from __future__ import annotations

from typing import Any

PINNED = "pinned"
PREFERRED = "preferred"
CLASS_ONLY = "class_only"
PROVIDER_INTENTS: tuple[str, ...] = (PINNED, PREFERRED, CLASS_ONLY)

#: The ``task_metadata`` key recording the last intent write (D9).
INTENT_AUDIT_KEY = "provider_intent_audit"

__all__ = [
    "CLASS_ONLY",
    "INTENT_AUDIT_KEY",
    "PINNED",
    "PREFERRED",
    "PROVIDER_INTENTS",
    "effective_intent",
    "intent_error",
    "narrows_catalog",
    "resolve_intent",
]


def effective_intent(task: Any) -> str:
    """The intent a reader acts on.

    ``pinned``/``preferred`` with no ``profile_id`` is ``class_only`` (D8):
    there is no provider left to have meant.  An unknown stored value (a
    row written by a newer build) reads as ``class_only`` too.
    """
    intent = str(getattr(task, "provider_intent", None) or CLASS_ONLY)
    if intent not in PROVIDER_INTENTS:
        return CLASS_ONLY
    if intent != CLASS_ONLY and not getattr(task, "profile_id", None):
        return CLASS_ONLY
    return intent


def narrows_catalog(task: Any) -> bool:
    """Does the task's own profile constrain the routing catalog (D8)?

    Only for ``preferred`` and ``pinned``: a ``class_only`` row is routed on
    its class, so its old placement constrains nothing.
    """
    return bool(getattr(task, "profile_id", None)) and effective_intent(task) != CLASS_ONLY


def intent_error(value: Any) -> str | None:
    """Why *value* is not a provider intent, or ``None``."""
    if value is None or value in PROVIDER_INTENTS:
        return None
    return f"provider_intent must be one of {', '.join(PROVIDER_INTENTS)}; got {value!r}"


def resolve_intent(
    requested: Any,
    *,
    pin: bool = False,
    profile_supplied: bool,
    profile_id: str | None,
) -> str:
    """The intent a write stores (D9).

    An explicit ``provider_intent`` wins; ``pin`` means ``pinned``.  Otherwise
    the default follows how the profile was chosen: a ``profile_id`` the
    caller supplied is ``preferred``; one a resolver supplied, or none, is
    ``class_only``.  With no profile at all the answer is always
    ``class_only`` -- there is no provider to have meant.
    """
    if not profile_id:
        return CLASS_ONLY
    if requested in PROVIDER_INTENTS:
        return str(requested)
    if pin:
        return PINNED
    return PREFERRED if profile_supplied else CLASS_ONLY
