"""Static source ports. Domains own locks, authority checks and application receipts.

snapshot(lock=True) must acquire the source row lock and reread it BEFORE any
ask lock. Methods share the caller's transaction; they must not open a nested
fresh transaction. A source implements either TransactionalAskAdapter or
OperationAskAdapter. CommandHandler constructs the registry from server-owned
instances; no command name/callback from text is executable here.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncConnection

from src.asks.models import (
    AskChoice,
    AskError,
    DecisionActor,
    DecisionIntent,
    JsonObject,
    OperationResult,
    SourceIdentity,
    SourceSnapshot,
)


@runtime_checkable
class AskAdapter(Protocol):
    source_kind: str
    policy: str
    version: str

    async def snapshot(
        self, source: SourceIdentity, *, conn: AsyncConnection, lock: bool = True
    ) -> SourceSnapshot: ...

    def choices(self, snapshot: SourceSnapshot) -> tuple[AskChoice, ...]: ...

    async def validate(
        self,
        snapshot: SourceSnapshot,
        actor: DecisionActor,
        intent: DecisionIntent,
        *,
        conn: AsyncConnection,
        accepted_decision: JsonObject | None = None,
    ) -> JsonObject:
        """Recheck audience/decider/source, returning the complete domain packet.

        On receipt replay, accepted_decision supplies the frozen decision: validate
        current read/decider authority without requiring the old effect to remain
        applicable. Replaying a receipt must never apply an effect again.

        Review adapters include all comments, immutable revision/hash, attachment
        manifest and dispositions. Failure raises AskError; no effects are allowed.
        """
        ...


class TransactionalAskAdapter(AskAdapter, Protocol):
    async def apply_on(self, decision: JsonObject, *, conn: AsyncConnection) -> OperationResult: ...


class OperationAskAdapter(AskAdapter, Protocol):
    async def start_operation(
        self, decision: JsonObject, operation: JsonObject
    ) -> OperationResult: ...

    async def reconcile_operation(
        self, decision: JsonObject, operation: JsonObject
    ) -> OperationResult: ...


class AdapterRegistry:
    def __init__(self, adapters: Iterable[AskAdapter] = ()):
        self._adapters: dict[tuple[str, str], AskAdapter] = {}
        for adapter in adapters:
            key = (adapter.source_kind, adapter.policy)
            if key in self._adapters or not adapter.version:
                raise ValueError(f"Duplicate or unversioned ask adapter: {key}")
            self._adapters[key] = adapter

    def get(self, kind: str, policy: str = "") -> AskAdapter:
        try:
            return self._adapters[(kind, policy)]
        except KeyError:
            raise AskError("ask.unsupported_source", "No registered human source policy") from None
