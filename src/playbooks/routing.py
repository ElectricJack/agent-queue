"""Inspect configured routing policy before a new task can be dispatched.

Pipeline callbacks run asynchronously. Their routing gates must be committed
with task creation, not attached later after a worker could have claimed it.
Cooldown and runner capacity delay execution; they do not waive routing.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, is_dataclass
import logging
from typing import Any

from src.playbooks.definition import CommandStep, PlaybookDefinition, step_targets
from src.playbooks.expressions import (
    ResolutionScope,
    ValueResolutionError,
    evaluate_condition,
    resolve_value,
)


logger = logging.getLogger(__name__)


_DEPRECATED_DEFAULT_ASSIGNMENT_RULES = (
    "task-created-routing",
    "worker-filed-triage",
)


def is_deprecated_default_assignment_entry(playbook, entry: str) -> bool:
    """Identify cached system-default rule entries superseded by the router."""

    scope = getattr(playbook, "scope", "")
    scope = getattr(scope, "value", scope)
    if (
        getattr(playbook, "id", "") != "default-pipeline"
        or getattr(playbook, "kind", "") != "pipeline"
        or scope != "system"
    ):
        return False
    return any(
        entry == rule_id or entry.startswith(f"{rule_id}-")
        for rule_id in _DEPRECATED_DEFAULT_ASSIGNMENT_RULES
    )


@dataclass(frozen=True, slots=True)
class CommandEffect:
    """One reachable V2 command with its event-resolved inputs."""

    command: str
    inputs: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _RoutingActivationSnapshot:
    rows: tuple[Mapping[str, Any], ...]
    artifact_store: Any


class _RoutingArtifactUnavailable(RuntimeError):
    """Admission cannot prove what the active routing policy would do."""


def install_routing_activation_snapshot(
    manager: Any,
    rows: Iterable[Mapping[str, Any]],
    *,
    artifact_store: Any,
) -> None:
    """Install an immutable activation view for synchronous task admission.

    The database read belongs before task creation opens its write transaction.
    Admission itself only verifies and reads content-addressed artifact files.
    """

    manager._routing_activation_snapshot = _RoutingActivationSnapshot(  # noqa: SLF001
        rows=tuple(dict(row) for row in rows),
        artifact_store=artifact_store,
    )


async def refresh_routing_activation_snapshot(
    manager: Any,
    db: Any,
    *,
    artifact_store: Any | None = None,
) -> None:
    """Refresh the synchronous admission view outside a task transaction."""

    if artifact_store is None:
        from src.playbooks.artifact_store import ArtifactStore

        config = manager._config  # noqa: SLF001 - manager owns the snapshot
        artifact_store = ArtifactStore(
            config.compiled_root,
            max_artifact_bytes=config.playbooks.v2_max_artifact_bytes,
        )
    try:
        rows = await db.list_playbook_activations(enabled_only=True)
    except Exception:  # noqa: BLE001 - an empty snapshot is the fail-closed state
        logger.exception("could not refresh the routing activation snapshot")
        rows = []
    install_routing_activation_snapshot(manager, rows, artifact_store=artifact_store)


def _scope_matches(row: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    scope = row.get("scope")
    identifier = row.get("scope_identifier") or ""
    if scope == "system":
        return True
    if scope == "project":
        return bool(event.get("project_id")) and identifier == event.get("project_id")
    if scope == "agent_type":
        return bool(event.get("project_id")) and identifier == event.get("agent_type")
    return False


def _trigger_matches(rule: Any, event: Mapping[str, Any], *, match_filter: bool) -> bool:
    if rule.trigger.event_type != "task.created":
        return False
    if not match_filter:
        return True
    for name, expected in (rule.trigger.filter or {}).items():
        actual = event.get(name)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    if rule.guard is None:
        return True
    try:
        return evaluate_condition(rule.guard, ResolutionScope(event=event))
    except ValueResolutionError:
        return False


def _loaded_activations(
    manager: Any, event: Mapping[str, Any]
) -> list[tuple[Mapping[str, Any], PlaybookDefinition]]:
    snapshot = getattr(manager, "_routing_activation_snapshot", None)
    if not isinstance(snapshot, _RoutingActivationSnapshot):
        raise _RoutingArtifactUnavailable("routing activation snapshot is not initialized")
    matching = [
        row
        for row in snapshot.rows
        if row.get("enabled") is True
        and row.get("health") == "ready"
        and row.get("active_artifact_sha256")
        and _scope_matches(row, event)
    ]
    if not matching:
        raise _RoutingArtifactUnavailable("no ready routing activation matches this event")

    loaded: list[tuple[Mapping[str, Any], PlaybookDefinition]] = []
    for row in matching:
        sha = str(row["active_artifact_sha256"])
        try:
            artifact = snapshot.artifact_store.load(sha)
        except Exception as exc:  # noqa: BLE001 - any unreadable active artifact is unsafe
            raise _RoutingArtifactUnavailable(f"active routing artifact {sha} is unavailable") from exc
        if artifact.id != row.get("playbook_id"):
            raise _RoutingArtifactUnavailable(
                f"activation names {row.get('playbook_id')!r}, artifact names {artifact.id!r}"
            )
        loaded.append((row, artifact))
    return loaded


def _artifact_command_effects(
    manager: Any,
    event: Mapping[str, Any],
    *,
    match_filter: bool = True,
) -> Iterator[CommandEffect]:
    """Yield command effects an enabled V2 activation would execute for *event*.

    This is a static, bounded walk. It loads immutable artifacts but never
    dispatches, creates a run, touches ``PlaybookEngine``, or opens a database
    connection; task creation calls it while already holding a write transaction.
    """

    loaded = _loaded_activations(manager, event)
    project_has_policy = any(
        row.get("scope") == "project"
        and any(rule.trigger.event_type == "task.created" for rule in artifact.rules)
        for row, artifact in loaded
    )
    for row, artifact in loaded:
        if project_has_policy and row.get("scope") == "system":
            continue
        for rule in artifact.rules:
            if not _trigger_matches(rule, event, match_filter=match_filter):
                continue
            pending = [rule.entry_step]
            visited: set[str] = set()
            while pending:
                step_id = pending.pop()
                if step_id in visited:
                    continue
                visited.add(step_id)
                step = artifact.steps.get(step_id)
                if step is None:
                    raise _RoutingArtifactUnavailable(
                        f"rule {rule.id!r} targets missing step {step_id!r}"
                    )
                if isinstance(step, CommandStep):
                    try:
                        inputs = {
                            name: resolve_value(value, ResolutionScope(event=event))
                            for name, value in step.inputs.items()
                        }
                    except ValueResolutionError as exc:
                        if not match_filter:
                            # Recovery asks whether the artifact has triage
                            # capability, not whether an old task event can be
                            # reconstructed. Keep walking past event-dependent
                            # commands to find the reusable ensure_task effect.
                            pending.extend(step_targets(step).values())
                            continue
                        raise _RoutingArtifactUnavailable(
                            f"command {step_id!r} inputs do not resolve for admission"
                        ) from exc
                    yield CommandEffect(step.command, inputs)
                pending.extend(step_targets(step).values())


def _playbooks_enabled(manager: Any) -> bool:
    if manager is None:
        return False
    return (
        getattr(getattr(getattr(manager, "_config", None), "playbooks", None), "enabled", True)
        is not False
    )


def requires_routing_gate(manager, task, event_extra=None) -> bool:
    """Whether a selected task-created rule attaches a routing gate to this task."""
    row = asdict(task) if is_dataclass(task) else dict(task)
    if event_extra and "parent_task_id" in event_extra:
        row["parent_task_id"] = event_extra["parent_task_id"]
    if (row.get("profile_id") or "").strip():
        return False
    if not _playbooks_enabled(manager):
        return False
    event = {**row, "task_id": row.get("id"), "task": row, **(event_extra or {})}
    try:
        effects = _artifact_command_effects(manager, event)
        for effect in effects:
            args = effect.inputs
            waiters = args.get("waiter_task_ids") or []
            if isinstance(waiters, str):
                waiters = [waiters]
            if (
                effect.command == "gate_create"
                and args.get("gate_type") == "routing"
                and row.get("id") in waiters
                and args.get("project_id") == row.get("project_id")
            ):
                return True
    except _RoutingArtifactUnavailable as exc:
        logger.warning("routing admission failed closed for task %s: %s", row.get("id"), exc)
        return True
    return False


def uses_default_triage(manager, project_id: str) -> bool:
    """Whether the selected routing pipeline uses the reusable project triage job.

    Recovery has gates, not the original event. Inspect rule capability without
    reapplying event-only filters; project scope and shadowing still apply.
    """
    if not _playbooks_enabled(manager):
        return False
    event = {"project_id": project_id}
    try:
        return any(
            effect.command == "ensure_task"
            and effect.inputs.get("profile_id") == "triage"
            and effect.inputs.get("dedup_key") == "triage-open"
            for effect in _artifact_command_effects(manager, event, match_filter=False)
        )
    except _RoutingArtifactUnavailable as exc:
        logger.warning("triage admission failed closed for project %s: %s", project_id, exc)
        return False
