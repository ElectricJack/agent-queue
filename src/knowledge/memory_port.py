"""Core-side port for a legacy semantic adapter (K12).

The aq-memory plugin owns embeddings, chunking and its own fixed Milvus
collection; it is a separate repository and is never imported here. This module
is the boundary that lets such a plugin serve core ranking without owning
authority:

- the plugin is reached only through the already-registered memory service, and
  only after both feature switches are on;
- the query, the core-derived ``scope_key`` and ``limit`` come from
  :class:`src.knowledge.providers.RetrievalRequest`, so there is no
  caller-controlled collection name, path, role or model scope;
- only rows naming an exact record, revision and chunk survive, and they are
  re-validated into :class:`RetrievalReference`, so legacy ``summary``,
  ``original``, ``content`` and tag fields cannot reach a caller;
- no write path exists on this object at all: ``kv_set``, ``fact_set``,
  ``save_document``, promotion and consolidation are unreachable from here.

Scope and summary are the two legacy behaviours the adapter must not inherit:
a foreign-scope row is dropped rather than trusted, and a summary is ranking
input at most, never served text.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from src.knowledge.providers import RetrievalReference, RetrievalRequest
from src.knowledge.registration import provider_id_of, provider_version_of
from src.records.models import RecordError

logger = logging.getLogger(__name__)

#: Rows are converted only when they name these exact identities.
IDENTITY_FIELDS = ("record_id", "revision_id", "chunk_id")


class MemorySemanticPort:
    """Adapts a loaded legacy memory service to the core ranking contract.

    ``memory_service`` is the plugin's own object (an implementation of
    :class:`src.plugins.services.MemoryServiceProtocol`). It is used for one
    scoped read call; its initialization, health and collection layout stay
    entirely with the plugin.
    """

    def __init__(
        self,
        memory_service: Any,
        *,
        provider_id: str,
        provider_version: str,
        provider_name: str = "aq-memory",
        top_k_cap: int = 100,
    ):
        if memory_service is None:
            raise RecordError("knowledge.provider_absent")
        if provider_id_of(provider_id) is None or provider_version_of(provider_version) is None:
            # The same bounded identity the registry accepts, so an adapter can
            # never register under a name core would later refuse.
            raise RecordError("knowledge.provider_invalid_id")
        if isinstance(top_k_cap, bool) or not isinstance(top_k_cap, int) or top_k_cap < 1:
            raise RecordError("record.invalid_input", "top_k_cap must be a positive integer")
        self._memory = memory_service
        self._provider_id = provider_id
        self._provider_version = provider_version
        self._provider_name = provider_name
        self._top_k_cap = top_k_cap

    # -- provider identity ------------------------------------------------

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def provider_version(self) -> str:
        return self._provider_version

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def available(self) -> bool:
        """Local flag only; the plugin decides what healthy means."""

        return bool(getattr(self._memory, "available", False))

    @property
    def deprecated(self) -> bool:
        return bool(getattr(self._memory, "deprecated", False))

    def handshake(self) -> dict:
        """Versioned read-only adapter handshake for an operator decision."""

        return {
            "adapter": "legacy-memory-semantic",
            "handshake_version": 1,
            "provider_id": self._provider_id,
            "provider_version": self._provider_version,
            "provider_name": self._provider_name,
            "read_only": True,
            "authoritative_writes": False,
            "extraction_fenced": True,
        }

    # -- ranking ----------------------------------------------------------

    async def search(self, request: RetrievalRequest) -> Sequence[RetrievalReference]:
        """Return at most ``request.limit`` references for the core-derived scope.

        The scope key is forwarded as an opaque label: the plugin maps it to its
        own collection. Anything the plugin returns that is not an exact
        reference for that scope's readable records is dropped here, and core
        reauthorizes every survivor again before it is served.
        """

        if not isinstance(request, RetrievalRequest):
            raise RecordError("record.invalid_input", "Invalid retrieval request")
        if (
            isinstance(request.limit, bool)
            or not isinstance(request.limit, int)
            or not 1 <= request.limit <= self._top_k_cap
        ):
            raise RecordError("record.invalid_input", "Retrieval limit is out of bounds")
        rows = await self._memory.search(
            request.scope_key,
            request.query,
            scope=request.scope_key,
            top_k=min(request.limit, self._top_k_cap),
        )
        return self._convert(rows, limit=request.limit, scope_key=request.scope_key)

    def _convert(self, rows: Any, *, limit: int, scope_key: str) -> list[RetrievalReference]:
        if not isinstance(rows, (list, tuple)):
            raise RecordError("record.invalid_input", "Legacy search returned no rows")
        references: list[RetrievalReference] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows[: limit * 2]:
            if not isinstance(row, dict):
                continue
            if any(row.get(field) in (None, "") for field in IDENTITY_FIELDS):
                # Legacy memory entries (KV, facts, pre-knowledge documents) have
                # no core identity, so they are not knowledge references.
                continue
            claimed = row.get("scope_key")
            if claimed is not None and claimed != scope_key:
                # A row tagged for another scope is never trusted or translated.
                continue
            try:
                reference = RetrievalReference.model_validate(
                    {
                        "record_id": row["record_id"],
                        "revision_id": row["revision_id"],
                        "chunk_id": row["chunk_id"],
                        "score": row.get("score"),
                        "provider_version": row.get("provider_version")
                        or self._provider_version,
                    }
                )
            except ValidationError:
                logger.debug("Dropped a legacy row that is not an exact reference")
                continue
            key = (str(reference.record_id), str(reference.revision_id), reference.chunk_id)
            if key in seen:
                continue
            seen.add(key)
            references.append(reference)
            if len(references) == limit:
                break
        return references


def legacy_adapter(
    memory_service: Any,
    *,
    provider_id: str = "aq-memory",
    provider_version: str,
) -> MemorySemanticPort:
    """Build the adapter a loaded aq-memory plugin registers as ``knowledge_retrieval``."""

    return MemorySemanticPort(
        memory_service,
        provider_id=provider_id,
        provider_version=provider_version,
    )
