"""Durable waits — the inert predicate a suspended run is parked on.

Package 3 child plan §10.  A wait is data, never code: it is read back from
the database after a restart, so ``match`` is a flat mapping of dotted event
field path to required literal and nothing in it can execute.

The race the design spec names — "an event cannot be lost between
registration and suspension" — is closed by construction rather than by
retry: ``WaitChangeSet`` travels with the snapshot into
``RunRepository.commit_boundary``, which applies both on one connection, so
there is no interval in which a run is suspended and its wait is invisible.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

WAIT_KINDS: frozenset[str] = frozenset({"event", "timer", "human", "agent_task"})

#: The wait kinds an ingested event may claim.  Only an ``event`` wait is
#: addressable by event dispatch: a ``timer`` wait ends at its deadline, a
#: ``human`` wait at an answer, an ``agent_task`` wait at its task's outcome.
#: Those three leave ``event_type`` and ``match`` empty, which the predicate
#: below would otherwise read as "any event whatsoever", so the kind — not the
#: emptiness of the predicate — is what decides addressability.
EVENT_ADDRESSABLE_WAIT_KINDS: frozenset[str] = frozenset({"event"})
WAIT_STATES: frozenset[str] = frozenset({"active", "claimed", "expired", "cleared"})

#: Sentinel for "this path is absent", distinct from a stored ``None``.
_MISSING = object()


@dataclass(frozen=True, slots=True)
class WaitSpec:
    """One durable suspension point of one run."""

    wait_id: str
    run_id: str
    step_id: str
    iteration: int = -1
    kind: str = "event"
    event_type: str = ""
    match: Mapping[str, Any] = field(default_factory=dict)
    deadline_at: float | None = None

    def __post_init__(self) -> None:
        if not self.wait_id:
            raise ValueError("wait_id is required")
        if self.kind not in WAIT_KINDS:
            raise ValueError(f"kind must be one of {sorted(WAIT_KINDS)}, got {self.kind!r}")
        if not isinstance(self.match, Mapping):
            raise TypeError("match must be a mapping of dotted field path to literal")

    @property
    def correlation_key(self) -> str:
        """Stable digest of (kind, event_type, match) for operator search."""
        payload = json.dumps(
            [self.kind, self.event_type, dict(self.match)],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "wait_id": self.wait_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "iteration": self.iteration,
            "kind": self.kind,
            "event_type": self.event_type,
            "match": dict(self.match),
            "deadline_at": self.deadline_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WaitSpec:
        return cls(
            wait_id=data["wait_id"],
            run_id=data["run_id"],
            step_id=data["step_id"],
            iteration=int(data.get("iteration", -1)),
            kind=data.get("kind", "event"),
            event_type=data.get("event_type", ""),
            match=dict(data.get("match") or {}),
            deadline_at=data.get("deadline_at"),
        )


@dataclass(frozen=True, slots=True)
class WaitClaim:
    """The outcome of claiming one wait — for an event, or for an expiry."""

    wait_id: str
    run_id: str
    step_id: str
    iteration: int
    kind: str
    snapshot_version: int
    claimed_event_id: str | None
    claimed_at: float
    expired: bool = False


@dataclass(frozen=True, slots=True)
class WaitChangeSet:
    """What one commit boundary changes about a run's waits.

    Applied ``clear_run_waits`` → ``clear_wait_ids`` → ``register``, so a step
    that finishes one wait and opens another in the same boundary cannot trip
    the ``uq_playbook_waits_active_step`` partial unique index.
    """

    register: tuple[WaitSpec, ...] = ()
    clear_wait_ids: tuple[str, ...] = ()
    clear_run_waits: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.register and not self.clear_wait_ids and not self.clear_run_waits


EMPTY_WAIT_CHANGES = WaitChangeSet()


class MatchableEvent(Protocol):
    """The only thing wait matching needs from Package 4's event object."""

    event_type: str
    event_id: str | None
    fields: Mapping[str, Any]


def _resolve(fields: Mapping[str, Any], path: str) -> Any:
    """Read a dotted path out of an event's fields, or ``_MISSING``."""
    current: Any = fields
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


def matches(spec: WaitSpec, event: MatchableEvent) -> bool:
    """Whether ``event`` satisfies ``spec``'s inert predicate.

    An absent path never matches — including against a required ``None``, so
    "the field is missing" and "the field is null" stay distinguishable.  A
    wait of a kind no event addresses never matches at all, whatever its
    predicate says.
    """
    if spec.kind not in EVENT_ADDRESSABLE_WAIT_KINDS:
        return False
    if spec.event_type and spec.event_type != event.event_type:
        return False
    for path, expected in spec.match.items():
        actual = _resolve(event.fields, path)
        if actual is _MISSING or actual != expected:
            return False
    return True


class WaitRepository(Protocol):
    """Declared here so Package 4 never imports the database package.

    ``conn`` is the atomicity seam: ``commit_boundary`` passes its own
    connection so registration and the snapshot advance commit or roll back
    together; with ``conn=None`` each method opens its own ``immediate()``.
    """

    async def register(
        self, wait: WaitSpec, snapshot_version: int, *, conn: AsyncConnection | None = None
    ) -> str: ...

    async def claim_for_event(
        self, event: MatchableEvent, *, now: float, limit: int = 100
    ) -> list[WaitClaim]: ...

    async def expire_due(self, now: float, *, limit: int = 100) -> list[WaitClaim]: ...

    async def clear_for_run(self, run_id: str, *, conn: AsyncConnection | None = None) -> int: ...

    async def list_active(self, run_id: str) -> list[WaitSpec]: ...
