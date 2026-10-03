"""Local registration for optional retrieval providers (K12).

A retrieval provider is a service a plugin registered while it was already
loaded. Registration here is a synchronous lookup of present local state: it
never imports a plugin, resolves an entry point, starts Milvus, builds
embeddings, reads configuration or performs network I/O, and it is not consulted
until both feature switches are on. An absent, unusable or deprecated provider is
simply absent, so the ordinary lexical path answers the request.

Selecting a provider, paying its cost and retiring it are explicit operator
decisions (approved plan sections 2 and 11, gate G5). The reserved service name
therefore holds one instance: core never races or ranks two providers, and a
second plugin claiming the name is reported by the plugin registry, whose
operator decides which plugin stays loaded.
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from src.records.models import RecordError

logger = logging.getLogger(__name__)

#: Plugin-facing service name for a retrieval provider (plugin -> core).
RETRIEVAL_SERVICE = "knowledge_retrieval"

PROVIDER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
MAX_VERSION_BYTES = 128


def provider_id_of(value) -> str | None:
    """Return a bounded printable provider id, or ``None`` when unusable."""
    if not isinstance(value, str) or not PROVIDER_ID_PATTERN.match(value):
        return None
    if not value.isprintable():
        return None
    return value


def provider_version_of(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    if not value.isprintable() or len(value.encode("utf-8")) > MAX_VERSION_BYTES:
        return None
    return value


@dataclass(frozen=True)
class RegisteredProvider:
    """A loaded provider that satisfies the core contract on its face.

    Attributes are read once, locally. ``provider`` is the object core calls for
    ranking; ``indexer`` is the optional derived-index service of the same
    provider. Neither is initialized, started or health-checked here.
    """

    provider_id: str
    provider_version: str
    provider: object
    indexer: object | None = None
    deprecated: bool = False

    def handshake(self) -> dict:
        """A versioned, read-only adapter handshake an operator can inspect.

        Activation is still an operator decision: nothing here sets
        ``knowledge.legacy_memory_mode`` or any feature switch.
        """

        return {
            "adapter": "knowledge-retrieval",
            "handshake_version": 1,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "deprecated": self.deprecated,
            "index_capable": self.indexer is not None,
            "authoritative_writes": False,
        }


def _is_deprecated(provider) -> bool:
    return bool(getattr(provider, "deprecated", False))


def _is_available(provider) -> bool:
    """Read the plugin's own local availability flag; a raising probe is absent."""
    try:
        return bool(getattr(provider, "available", True))
    except Exception:  # noqa: BLE001 - a raising probe is an absent provider, not a crash
        return False


def _has_search(provider) -> bool:
    search = getattr(provider, "search", None)
    return callable(search) and inspect.iscoroutinefunction(search)


class RetrievalProviderRegistry:
    """Local view of the providers a loaded plugin has registered.

    ``get_service`` is the plugin-registry lookup for :data:`RETRIEVAL_SERVICE`;
    it is a dict read, never an import or a load. Tests and operators can pass
    any callable with that shape.
    """

    def __init__(self, get_service: Callable[[str], object]):
        self._get_service = get_service

    def registered(self) -> tuple[RegisteredProvider, ...]:
        """Every registered provider that satisfies the contract on its face."""
        try:
            service = self._get_service(RETRIEVAL_SERVICE)
        except Exception:  # noqa: BLE001 - absence is the answer; never fail a lexical read
            logger.warning("Retrieval provider service lookup failed; treating as absent")
            return ()
        if service is None:
            return ()
        identity = provider_id_of(getattr(service, "provider_id", None))
        version = provider_version_of(getattr(service, "provider_version", None))
        if identity is None or version is None:
            logger.warning("Retrieval provider has an unusable id or version; treating as absent")
            return ()
        if not _has_search(service):
            logger.warning("Retrieval provider %r exposes no async search; treating as absent",
                           identity)
            return ()
        indexer = getattr(service, "indexer", None)
        return (
            RegisteredProvider(
                provider_id=identity,
                provider_version=version,
                provider=service,
                indexer=indexer if _has_index(indexer) else None,
                deprecated=_is_deprecated(service),
            ),
        )

    def usable(self) -> tuple[RegisteredProvider, ...]:
        """Registered providers that are available, current and not deprecated."""
        return tuple(
            entry for entry in self.registered() if not entry.deprecated and _is_available(entry.provider)
        )

    def lookup(self):
        """The one usable provider, or ``None``.

        Cost, quality and scope selection are operator decisions, so core serves
        only a single registered provider and reports absence otherwise.
        """

        usable = self.usable()
        return usable[0].provider if len(usable) == 1 else None

    def require(self, identity: str) -> RegisteredProvider:
        """Return a usable provider by id, or refuse with a stable reason code."""
        entry = provider_id_of(identity)
        if entry is None:
            raise RecordError("knowledge.provider_invalid_id")
        for candidate in self.registered():
            if candidate.provider_id != entry:
                continue
            if candidate.deprecated:
                raise RecordError("knowledge.provider_deprecated")
            if not _is_available(candidate.provider):
                raise RecordError("knowledge.provider_unavailable")
            return candidate
        raise RecordError("knowledge.provider_unregistered")


def _has_index(indexer) -> bool:
    return callable(getattr(indexer, "index", None)) and callable(
        getattr(indexer, "erase", None)
    )


def provider_registry(registry) -> RetrievalProviderRegistry:
    """Bind a plugin registry to the local provider view without loading anything."""

    return RetrievalProviderRegistry(lambda name: registry.get_service(name))
