"""Shared strict parsing and evidence handling, never a catch-all adapter."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select

from src.waits.conditions import (
    Condition,
    DeadlinePolicy,
    Edge,
    NodeKey,
    Observation,
    Scope,
    Snapshot,
    SourceEvent,
    VerifiedEvidence,
    WaitError,
    WaitPolicy,
)


def invalid(message="one typed condition is required"):
    raise WaitError("wait.condition_required", message)


def unresolved(message="source could not be verified", **details):
    raise WaitError("wait.source_unresolved", message, **details)


def foreign():
    # Deliberately no object identity or project in either message or details.
    raise WaitError("wait.out_of_scope", "condition is outside the principal's scope")


def fields(raw, *, required, optional=()):
    if not isinstance(raw, Mapping) or set(raw) - {"kind", "generation", *required, *optional}:
        invalid()
    if not set(required) <= set(raw):
        invalid("condition identity is incomplete")
    if "generation" in raw:
        integer(raw["generation"])


def string(value):
    if not isinstance(value, str) or not value.strip():
        invalid("condition identity must be a nonempty string")
    return value.strip()


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        invalid("condition counter must be a nonnegative integer")
    return value


async def row(conn, table, *where):
    result = (await conn.execute(select(table).where(*where))).mappings().first()
    return dict(result) if result else None


class Adapter:
    kind: str

    async def snapshot(self, conn, cond: Condition) -> Snapshot | None:
        raise NotImplementedError

    async def authorise(self, conn, cond: Condition, scope: Scope) -> Condition:
        return cond.bind(
            generation=cond.generation,
            project_id=scope.project_id,
            owner_kind=scope.owner_kind,
            owner_id=scope.owner_id,
            session_id=scope.session_id,
            claim_epoch=scope.claim_epoch,
        )

    async def verify(self, conn, cond: Condition, scope: Scope) -> VerifiedEvidence:
        cond = await self.authorise(conn, cond, scope)
        snapshot = await self.snapshot(conn, cond)
        if snapshot is None:
            unresolved()
        if snapshot.project_id != scope.project_id:
            foreign()
        if cond.generation is not None and cond.generation != snapshot.generation:
            unresolved("source generation has changed")
        cond = cond.bind(
            generation=snapshot.generation,
            source_class=snapshot.source_class,
            blockers=[{"key": b.key, "project_id": b.project_id} for b in snapshot.blockers],
        )
        source = self.source_key(cond)
        observation = snapshot.observation
        if observation.state == "unavailable":
            unresolved()
        return VerifiedEvidence(
            cond,
            source,
            observation,
            f"{source.identity}@{source.generation}:{observation.revision}",
        )

    async def observe(self, conn, cond: Condition) -> Observation:
        snapshot = await self.snapshot(conn, cond)
        if snapshot is None or snapshot.project_id != cond.context.get("project_id"):
            return Observation("missing", "unavailable")
        if snapshot.generation != cond.generation:
            return Observation(
                snapshot.observation.revision,
                "unavailable",
                facts={
                    "reason": "source_changed",
                },
            )
        return snapshot.observation

    def edges(self, cond: Condition, waiter: NodeKey) -> list[Edge]:
        return [Edge(waiter, NodeKey(**b)) for b in cond.context.get("blockers", ())]

    def deadline_policy(self, cond: Condition, policy: WaitPolicy) -> DeadlinePolicy:
        return DeadlinePolicy(policy.escalation_seconds(self.kind))

    def project_result(self, cond: Condition, event: SourceEvent) -> str:
        """Project a generation-matched event after the caller proves satisfaction."""
        if event.source != self.source_key(cond):
            unresolved("event belongs to a different source generation")
        return event.outcome_ref

    def source_class(self, cond: Condition) -> str:
        return cond.context["source_class"]
