"""Optional ranking over core-owned knowledge, with no provider initialization on import.

This is a consumable boundary, not plugin registration or context assembly. A
provider returns references only. Every served revision is read again through
KnowledgeService; provider chunks never supply text, permissions or authority.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.commands.principal import ExecutionPrincipal
from src.config import MemoryConfig
from src.knowledge.search import validate_search_bounds
from src.knowledge.service import KnowledgeService
from src.records.models import RecordError

MAX_REFERENCES = 100


class RetrievalReference(BaseModel):
    """Untrusted index identity and ranking metadata; no snippets or state fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")

    record_id: UUID
    revision_id: UUID
    chunk_id: str = Field(min_length=1, max_length=128, strict=True)
    score: float = Field(allow_inf_nan=False, strict=True)
    provider_version: str = Field(min_length=1, max_length=128, strict=True)

    @field_validator("chunk_id", "provider_version")
    @classmethod
    def bounded_metadata(cls, value: str) -> str:
        if not value.strip() or not value.isprintable() or len(value.encode()) > 128:
            raise ValueError("Invalid ranking metadata")
        return value


@dataclass(frozen=True)
class RetrievalRequest:
    """Core-derived scope hint. It is never proof that a candidate is readable."""

    query: str
    scope_key: str
    include_shared_global: bool
    limit: int


class RetrievalProvider(Protocol):
    async def search(self, request: RetrievalRequest) -> Sequence[RetrievalReference]:
        """Return at most request.limit references, ordered by relevance.

        Implementations must use their own fixed managed index, never a caller's
        collection/path. Index writes, embeddings and outbound policy belong to
        the adapter. No task, record, authority or delivery writes are permitted.
        """
        ...


class KnowledgeRetrieval:
    """Bounded optional search and exact reads, independent of task execution.

    provider_lookup is a synchronous, local registry lookup. It must not import
    or initialize a plugin or perform I/O. It is not invoked until an authorized
    semantic request passes both feature switches. Final K12 owns registration.
    """

    def __init__(
        self,
        service: KnowledgeService,
        memory_config: MemoryConfig,
        *,
        provider_lookup: Callable[[], RetrievalProvider | None] | None = None,
        timeout_seconds: float = 0.5,
    ):
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 5
        ):
            raise ValueError("Provider timeout must be greater than zero and at most five seconds")
        self.service = service
        self.memory_config = memory_config
        self.provider_lookup = provider_lookup
        self.timeout_seconds = timeout_seconds
        self._provider_slots = asyncio.Semaphore(2)

    def _semantic_enabled(self) -> bool:
        return (
            self.service.config.enabled is True
            and self.service.config.semantic.enabled is True
            and self.memory_config.enabled is True
        )

    async def search(
        self,
        *,
        principal: ExecutionPrincipal,
        project_id,
        query: str = "",
        limit: int = 25,
        semantic: bool = False,
    ) -> dict:
        """Discover metadata; lexical is the default and every fallback is fresh.

        Returned diagnostics are fixed reason codes, never provider errors or
        rejected identities/counts. Pagination/filtering remain on core search.
        """
        validate_search_bounds(
            query=query,
            category=None,
            lifecycle=None,
            verification=None,
            include_retired=False,
            include_disputed=False,
            limit=limit,
        )
        if type(semantic) is not bool:
            raise RecordError("record.invalid_input", "semantic must be a boolean")
        args = dict(principal=principal, project_id=project_id, query=query, limit=limit)

        async def lexical(reason):
            result = await self.service.search(**args)
            return {**result, "retrieval": {"mode": "lexical", "reason": reason}}

        if not semantic:
            return await lexical("lexical_requested")
        if not self._semantic_enabled():
            return await lexical("semantic_disabled")
        if self.provider_lookup is None:
            return await lexical("provider_absent")

        access = await self.service._transaction(
            lambda conn: self.service._access(conn, principal, "knowledge_search", project_id)
        )
        request = RetrievalRequest(
            query=query,
            scope_key=access.scope_key,
            include_shared_global=access.global_enabled and access.scope_key != "global",
            limit=limit,
        )
        unavailable_reason = None
        try:
            # Never hold a database connection across optional provider work.
            async with asyncio.timeout(self.timeout_seconds):
                async with self._provider_slots:
                    if not self._semantic_enabled():
                        unavailable_reason = "semantic_disabled"
                    else:
                        provider = self.provider_lookup()
                        if provider is None:
                            unavailable_reason = "provider_absent"
                        else:
                            candidates = await provider.search(request)
        except TimeoutError:
            return await lexical("provider_timeout")
        except Exception:
            # Do not echo arbitrary provider messages (which may contain secrets).
            return await lexical("provider_unavailable")
        if unavailable_reason:
            return await lexical(unavailable_reason)
        if not self._semantic_enabled():
            return await lexical("semantic_disabled")
        if not isinstance(candidates, (list, tuple)) or len(candidates) > limit:
            return await lexical("provider_invalid_result")
        items = await self.hydrate_references(
            candidates, principal=principal, project_id=project_id
        )
        if not items:
            return await lexical("no_eligible_references")
        return dict(
            success=True,
            outcome="read",
            items=items,
            next_cursor=None,
            retrieval={"mode": "semantic", "reason": "provider_references"},
        )

    async def hydrate_references(self, references, *, principal, project_id) -> list[dict]:
        """Return safe current summaries for bounded references in provider order.

        Invalid, private, unknown, redacted and ineligible references disappear
        without exposing their identities. Duplicate chunks yield one summary.
        Chunk IDs are opaque ranking metadata, never content selectors.
        """
        if not isinstance(references, (list, tuple)) or len(references) > MAX_REFERENCES:
            raise RecordError("record.invalid_input", "At most 100 references are allowed")
        pins = []
        for value in references:
            try:
                pins.append(RetrievalReference.model_validate(value))
            except ValidationError:
                continue

        async def hydrate(conn):
            await self.service._access(conn, principal, "knowledge_search", project_id)
            items, seen = [], set()
            for pin in pins:
                key = (pin.record_id, pin.revision_id)
                if key in seen:
                    continue
                try:
                    shown = await self.service.show_on(
                        f"record:{pin.record_id}",
                        revision_id=str(pin.revision_id),
                        principal=principal,
                        project_id=project_id,
                        conn=conn,
                    )
                except RecordError as exc:
                    if exc.code not in {
                        "record.not_found",
                        "record.revision_unavailable",
                        "record.revision_redacted",
                        "record.invalid_input",
                        "record.forbidden",
                    }:
                        raise
                    continue
                doc = shown["snapshot"]
                if (
                    shown["revision_id"] != shown["current_revision_id"]
                    or doc["lifecycle"] != "active"
                    or doc["verification"] == "disputed"
                ):
                    continue
                seen.add(key)
                item = {
                    name: shown[name]
                    for name in (
                        "record_id",
                        "revision_id",
                        "knowledge_alias",
                        "kind",
                        "scope_key",
                        "sequence",
                        "content_sha256",
                        "hash_version",
                        "stale",
                        "stale_reason",
                    )
                }
                item.update(
                    {
                        name: doc[name]
                        for name in (
                            "title",
                            "summary",
                            "category",
                            "lifecycle",
                            "verification",
                            "tags",
                        )
                    }
                )
                item["authoritative"] = bool(shown["authority"])
                item["ranking"] = dict(
                    chunk_id=pin.chunk_id, score=pin.score, provider_version=pin.provider_version
                )
                items.append(item)
            return items

        return await self.service._transaction(hydrate)

    async def read(self, reference, *, principal, project_id) -> dict:
        """Read the exact core revision, including eligible historical evidence.

        Reindexing and chunk changes cannot substitute the current revision.
        Current permissions and tombstones still apply. No provider is consulted.
        This method does not attach, cite or deliver the returned content.
        """
        try:
            pin = RetrievalReference.model_validate(reference)
        except ValidationError:
            raise RecordError("record.invalid_input", "Invalid retrieval reference") from None
        return await self.service.show(
            identity=f"record:{pin.record_id}",
            revision_id=str(pin.revision_id),
            principal=principal,
            project_id=project_id,
        )
