"""Resolved provider/model policy for a profile, per surface.

A profile's ``default_class`` names the *intelligence class* on every
surface, and that class's per-provider mapping supplies the *model*.  What
fixes the *provider* is not the same on both surfaces, and conflating them
is what made a playbook LLM step and its AI card disagree:

* **Session launch** (:mod:`src.sessions.spec`) runs a CLI, and the
  profile's ``harness`` names which one — so the harness fixes the
  provider.  :func:`intelligence_for` is that answer.
* **The direct LLM path** (:mod:`src.llm.spec`) is a headless API call with
  no CLI, and ``llm.provider`` fixes the provider for all of it.  The
  ``llm:`` config carries a *single* ``api_key`` / ``base_url`` pair bound
  to that provider, so a per-profile provider there would hand one
  provider's credentials to another's adapter.
  :func:`direct_call_intelligence_for` is that answer, and it delegates to
  :func:`~src.llm.spec.resolve_call` rather than re-deriving, so a
  read-only surface cannot state a provider or model that the call would
  not use.

Both are pure functions, so a read-only surface — the semantic graph's AI
cards — can state the policy without building a session spec or holding a
harness registry.

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
    """Resolve *profile*'s class, provider and model for a **session** launch.

    The profile's harness fixes the provider here; a headless direct-path
    call resolves through :func:`direct_call_intelligence_for` instead.

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


def direct_call_intelligence_for(
    profile: Any,
    classes: Mapping[str, Any] | None = None,
    llm_config: Any | None = None,
) -> ProfileIntelligence:
    """Resolve what a **direct-path** call on *profile* actually runs on.

    This is the surface :class:`~src.playbooks.definition.LlmStep` executes
    on: :mod:`src.playbooks.executors.llm` builds an
    :class:`~src.llm.spec.LLMCallSpec` carrying the profile's
    ``default_class`` and nothing else, and the ``llm:`` config supplies the
    provider.  Rather than restate that resolution, this builds the same
    spec and hands it to the same :func:`~src.llm.spec.resolve_call`, so the
    card inherits the executor's fallbacks (``llm.default_class`` when the
    profile declares no class, ``llm.model`` when the class has no slice for
    the provider) and cannot drift from it.

    ``classes`` or ``llm_config`` of ``None`` means "no snapshot available":
    the profile's class is still named, but nothing is guessed about the
    provider or model.
    """
    class_id = str(getattr(profile, "default_class", "") or "").strip()
    if classes is None or llm_config is None:
        return ProfileIntelligence(intelligence_class=class_id or None)

    from src.llm.spec import LLMCallSpec, resolve_call

    resolved = resolve_call(
        LLMCallSpec(intelligence_class=class_id or None),
        llm_config,
        dict(classes),
    )
    return ProfileIntelligence(
        intelligence_class=class_id or str(getattr(llm_config, "default_class", "") or "").strip()
        or None,
        provider=resolved.provider or None,
        model=resolved.model or None,
    )


__all__ = [
    "HARNESS_PROVIDERS",
    "ProfileIntelligence",
    "direct_call_intelligence_for",
    "intelligence_for",
    "provider_for_harness",
]
