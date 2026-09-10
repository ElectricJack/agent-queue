"""The installer's step protocol and registry.

A step is the unit the contract's protocol table describes: an immutable id, a
declared input-schema version, a dependency list, an adapter owner, and one of
four terminal states.  Platform, database and provider adapters
(``noble-apex.3`` and later) do not subclass an engine class — they build
:class:`StepSpec` values and register them, which keeps the engine free of any
knowledge of what a step actually does.

Two callables carry the idempotence contract:

``verify``
    A read-only check of the step's *observable condition*.  The engine calls
    it on a rerun before it calls ``run``, which is how "rerunning is safe and
    does not duplicate resources" is enforced by the engine rather than
    re-argued in every adapter.  It returns ``True`` when the condition still
    holds and ``False`` when it is gone (the engine then runs the step again).
    A step that is *itself* read-only may instead return the
    :class:`~src.install.results.StepResult` it observed: the engine adopts it,
    stamps ``detail["revalidated"]``, and the step's detail survives the rerun
    instead of being flattened to "still true".  That matters for any detail a
    reader consumes — the closing summary's data locations and dashboard are
    built from exactly that.
``run``
    The action.  It may mutate only when :attr:`StepSpec.mutating` is true, and
    the engine will not call it in a dry run when it is.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .platform import PlatformFacts, SupportVerdict
from .results import ResourceRecord, StepResult, merge_resource


class InstallPlanError(ValueError):
    """A registry could not be ordered: unknown dependency, cycle, duplicate id."""


@dataclass(slots=True)
class StepContext:
    """Everything a step is allowed to see.

    The context carries no secrets and no live database handle by design: a
    step that needs a credential asks the human for it through a ``needs_user``
    result, and schema work belongs to the daemon/operator path, never here.
    """

    support: SupportVerdict
    options: Mapping[str, Any]
    dry_run: bool
    interactive: bool
    #: Resources recorded by earlier steps on this run *and* on previous runs,
    #: keyed by ``(kind, id)`` — this is what lets a rerun reuse rather than
    #: recreate.
    resources: Mapping[tuple[str, str], ResourceRecord] = field(default_factory=dict)
    #: Terminal states of the steps that already ran, keyed by step id.
    completed: Mapping[str, str] = field(default_factory=dict)

    @property
    def facts(self) -> PlatformFacts:
        return self.support.facts

    def option(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)

    def existing_resource(self, kind: str, resource_id: str) -> ResourceRecord | None:
        return self.resources.get((kind, resource_id))


StepRunner = Callable[[StepContext], StepResult]
#: ``True``/``False`` for the observable condition, or the result a read-only
#: step observed while verifying itself.  See this module's docstring.
StepVerifier = Callable[[StepContext], bool | StepResult]


@dataclass(frozen=True, slots=True)
class StepSpec:
    """The immutable declaration of one installer step."""

    id: str
    title: str
    run: StepRunner
    description: str = ""
    depends_on: tuple[str, ...] = ()
    #: Optional capability gate.  When set, the step runs only if the caller
    #: selected that capability; otherwise it is recorded ``skipped`` with the
    #: reason preserved, so a later run may select it.
    capability: str | None = None
    #: True when the step changes the host.  Mutating steps need consent in
    #: interactive mode, an explicit approval in unattended mode, and are never
    #: executed in a dry run.
    mutating: bool = False
    consent_prompt: str | None = None
    #: Read-only revalidation.  Returns a bool, or the ``StepResult`` it
    #: observed when the step is read-only enough to verify by running.
    verify: StepVerifier | None = None
    owner: str = "engine"
    input_schema_version: int = 1

    def __post_init__(self) -> None:
        if not self.id:
            raise InstallPlanError("step id must be non-empty")
        if self.mutating and not self.consent_prompt:
            object.__setattr__(
                self,
                "consent_prompt",
                f"{self.title}?",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "depends_on": list(self.depends_on),
            "capability": self.capability,
            "mutating": self.mutating,
            "owner": self.owner,
            "input_schema_version": self.input_schema_version,
        }


class StepRegistry:
    """An ordered, validated collection of steps.

    Registration order is preserved, and the topological sort is stable within
    it, so two runs of the same registry always execute the same sequence —
    the plan a ``--dry-run`` printed is the plan the real run follows.
    """

    def __init__(self, steps: Iterable[StepSpec] = ()) -> None:
        self._steps: dict[str, StepSpec] = {}
        for step in steps:
            self.register(step)

    def register(self, step: StepSpec) -> StepSpec:
        if step.id in self._steps:
            raise InstallPlanError(f"duplicate step id: {step.id}")
        self._steps[step.id] = step
        return step

    def extend(self, steps: Iterable[StepSpec]) -> None:
        for step in steps:
            self.register(step)

    def __contains__(self, step_id: object) -> bool:
        return step_id in self._steps

    def __len__(self) -> int:
        return len(self._steps)

    def __iter__(self) -> Iterator[StepSpec]:
        return iter(self.ordered())

    def get(self, step_id: str) -> StepSpec:
        try:
            return self._steps[step_id]
        except KeyError:
            raise InstallPlanError(f"unknown step: {step_id}") from None

    def ids(self) -> tuple[str, ...]:
        return tuple(self._steps)

    def capabilities(self) -> tuple[str, ...]:
        """Every capability any registered step is gated on, in step order."""
        seen: dict[str, None] = {}
        for step in self._steps.values():
            if step.capability:
                seen.setdefault(step.capability, None)
        return tuple(seen)

    def ordered(self) -> tuple[StepSpec, ...]:
        """Return the steps in dependency order.

        Kahn's algorithm over the registration order.  Unknown dependencies and
        cycles are reported by name: an adapter that mistypes a dependency id
        must fail at plan time with the id it wrote, not at run time with a
        missing step.
        """
        unknown = {
            f"{step.id} -> {dependency}"
            for step in self._steps.values()
            for dependency in step.depends_on
            if dependency not in self._steps
        }
        if unknown:
            raise InstallPlanError(
                "step dependencies name unregistered steps: " + ", ".join(sorted(unknown))
            )

        remaining = {step_id: set(step.depends_on) for step_id, step in self._steps.items()}
        ordered: list[StepSpec] = []
        while remaining:
            ready = [
                step_id
                for step_id in self._steps
                if step_id in remaining and not remaining[step_id]
            ]
            if not ready:
                raise InstallPlanError(
                    "step dependency cycle among: " + ", ".join(sorted(remaining))
                )
            for step_id in ready:
                ordered.append(self._steps[step_id])
                del remaining[step_id]
            for pending in remaining.values():
                pending.difference_update(ready)
        return tuple(ordered)

    def dependents_of(self, step_id: str, *, include_self: bool = True) -> tuple[str, ...]:
        """Return *step_id* and everything that transitively depends on it.

        This is what ``--restart-from`` invalidates: the contract says a
        restart invalidates "that step and its dependents only", so an
        unrelated completed step is never re-run.
        """
        self.get(step_id)
        selected = {step_id}
        for step in self.ordered():
            if selected.intersection(step.depends_on):
                selected.add(step.id)
        order = [step.id for step in self.ordered() if step.id in selected]
        if not include_self:
            order = [candidate for candidate in order if candidate != step_id]
        return tuple(order)

    def describe(self) -> list[dict[str, Any]]:
        return [step.to_dict() for step in self.ordered()]


def merge_resources(
    existing: Sequence[ResourceRecord], new: Iterable[ResourceRecord]
) -> tuple[ResourceRecord, ...]:
    """Merge resource records by ``(kind, id)``, newest observation winning.

    Deduplication here is the mechanical half of "a rerun does not duplicate
    resources": even a step that re-reports what it created cannot grow the
    owned-resource list.  Ownership is the one field the newest writer does not
    get to lower — see :func:`~src.install.results.merge_resource`.
    """
    merged: dict[tuple[str, str], ResourceRecord] = {record.key: record for record in existing}
    for record in new:
        previous = merged.get(record.key)
        merged[record.key] = record if previous is None else merge_resource(previous, record)
    return tuple(merged.values())
