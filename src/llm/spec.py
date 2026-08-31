"""LLMCallSpec — what a caller asks for — and its resolution against config and
intelligence classes (spec §3.1)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.config import LLMConfig, normalize_llm_provider
from src.intelligence_classes import IntelligenceClass, resolve_class

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMCallSpec:
    provider: str | None = None  # "anthropic" | "google" | "openai" (legacy ids accepted)
    model: str | None = None  # explicit model id; wins over intelligence_class
    intelligence_class: str | None = None  # e.g. "fast-low"; resolved per provider
    max_tokens: int | None = None
    caller: str = "llm"  # logged; e.g. "playbook:memory-consolidation"


@dataclass(frozen=True)
class ResolvedCall:
    provider: str
    model: str
    base_url: str
    api_key: str
    max_tokens: int
    extras: dict = field(default_factory=dict)  # class slice minus "model"
    caller: str = "llm"

    @property
    def cache_key(self) -> tuple[str, str, str, tuple]:
        return (
            self.provider,
            self.model,
            self.base_url,
            tuple(sorted(self.extras.items())),
        )


def resolve_call(
    spec: LLMCallSpec,
    config: LLMConfig,
    classes: dict[str, IntelligenceClass],
) -> ResolvedCall:
    """Resolution order: spec.model > intelligence class > config.model > adapter
    default."""
    provider = normalize_llm_provider(spec.provider or config.provider)
    model = spec.model or ""
    extras: dict = {}

    if not model:
        class_id = spec.intelligence_class or config.default_class
        if class_id:
            cls = classes.get(class_id)
            if cls is None:
                logger.warning(
                    "llm: unknown intelligence class %r — falling back", class_id
                )
            else:
                slice_ = resolve_class(cls, provider)
                if not slice_:
                    logger.warning(
                        "llm: intelligence class %r has no entry for provider %r — "
                        "falling back",
                        class_id,
                        provider,
                    )
                else:
                    model = str(slice_.pop("model", "") or "")
                    extras = slice_
    if not model:
        model = config.model

    return ResolvedCall(
        provider=provider,
        model=model,
        base_url=config.base_url,
        api_key=config.api_key,
        max_tokens=spec.max_tokens or config.max_tokens,
        extras=extras,
        caller=spec.caller,
    )


def spec_from_llm_config(
    d: dict | None, *, caller: str, max_tokens: int | None = None
) -> LLMCallSpec:
    """Build a spec from a playbook-style ``llm_config`` mapping.  Keys other than
    provider / model / intelligence_class / max_tokens are ignored."""
    d = d or {}
    mt = d.get("max_tokens", max_tokens)
    return LLMCallSpec(
        provider=d.get("provider") or None,
        model=d.get("model") or None,
        intelligence_class=d.get("intelligence_class") or None,
        max_tokens=int(mt) if mt else None,
        caller=caller,
    )
