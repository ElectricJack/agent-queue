"""Resolved provider/model policy for a profile.

Session launch (:mod:`src.sessions.spec`) and the direct LLM path
(:mod:`src.llm.spec`) both answer the same question from the same three
inputs: the profile's harness fixes the *provider*, the profile's
``default_class`` names the *intelligence class*, and that class's
per-provider mapping supplies the *model*.  This module is that answer as
one pure function, so a read-only surface — the semantic graph's AI cards —
can state the policy without building a session spec or holding a harness
registry.

The class snapshot is injected rather than loaded: callers already hold the
orchestrator's live :class:`~src.intelligence_classes.registry.IntelligenceClassRegistry`,
and a projection must not do I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

#: Harness id -> the provider key that indexes ``IntelligenceClass.mapping``.
#: The same table as :func:`src.sessions.spec._infer_provider_from_harness`,
#: keyed by the profile's harness *id* rather than a resolved harness object,
#: because the surfaces here never load the vault's harness files.
HARNESS_PROVIDERS: Final[dict[str, str]] = {
    "claude": "anthropic",
    "codex": "openai",
    "gemini": "google",
}


@dataclass(frozen=True)
class ProfileIntelligence:
    """What a profile resolves to. ``None`` means "not determined", never a guess."""

    intelligence_class: str | None = None
    provider: str | None = None
    model: str | None = None


def provider_for_harness(harness: str | None) -> str:
    """Map a profile harness id to the intelligence-class provider key."""
    return HARNESS_PROVIDERS.get((harness or "").strip(), "")


def intelligence_for(
    profile: Any, classes: Mapping[str, Any] | None = None
) -> ProfileIntelligence:
    """Resolve *profile*'s class, provider and model against a class snapshot.

    Every field degrades independently: a profile with no ``default_class``
    still reports its provider, and an unknown class or a class with no slice
    for the provider reports no model rather than a fallback.  ``classes``
    of ``None`` means "no snapshot available" and yields no model.
    """
    class_id = str(getattr(profile, "default_class", "") or "").strip()
    harness = str(getattr(profile, "harness", "") or "").strip()
    provider = provider_for_harness(harness)
    model = ""
    if class_id and provider and classes is not None:
        cls = classes.get(class_id)
        if cls is not None:
            from src.intelligence_classes import resolve_class

            # Codex account models are a separate namespace from the OpenAI
            # API defaults; the optional slice only applies to that CLI.
            slice_ = resolve_class(cls, "codex") if harness == "codex" else {}
            if not slice_:
                slice_ = resolve_class(cls, provider)
            model = str(slice_.get("model") or "").strip()
    return ProfileIntelligence(
        intelligence_class=class_id or None,
        provider=provider or None,
        model=model or None,
    )


__all__ = [
    "HARNESS_PROVIDERS",
    "ProfileIntelligence",
    "intelligence_for",
    "provider_for_harness",
]
