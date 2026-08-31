"""Pure projection of configured workers into launch-equivalent identities."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

from src.agents.configuration import apply_agent_overrides
from src.models import AgentState


@dataclass(frozen=True)
class ExecutionIdentity:
    profile_id: str
    harness: str
    provider: str
    model: str
    intelligence_class: str
    reasoning_effort: str


def execution_type_key(identity: ExecutionIdentity) -> str:
    payload = json.dumps(asdict(identity), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExecutionCatalog:
    types: dict[str, ExecutionIdentity]
    members: dict[str, tuple[str, ...]]
    idle_counts: dict[str, int]
    diagnostics: tuple[dict[str, str], ...]


async def resolve_execution_catalog(
    db, project_id, *, builder, harness_registry
) -> ExecutionCatalog:
    """Resolve every eligible worker exactly once, without a target task."""
    agents = await db.list_agents(include_deleted=True)
    profiles = {profile.id: profile for profile in await db.list_profiles()}
    sessions = await db.list_sessions(live_only=True)
    occupied = {session.agent_id for session in sessions if session.agent_id}
    grouped: dict[str, list[str]] = {}
    identities: dict[str, ExecutionIdentity] = {}
    idle: dict[str, int] = {}
    diagnostics: list[dict[str, str]] = []

    for agent in sorted(agents, key=lambda row: row.id):
        if (
            agent.role != "worker"
            or not agent.enabled
            or agent.deleted_at is not None
            or agent.state == AgentState.RETIRED
        ):
            continue
        scoped_id = f"project:{project_id}:{agent.profile_id}"
        profile = profiles.get(scoped_id) or profiles.get(agent.profile_id)
        if profile is None:
            diagnostics.append(
                {
                    "agent_id": agent.id,
                    "code": "missing_profile",
                    "message": f"profile {agent.profile_id!r} is unavailable",
                }
            )
            continue
        effective = apply_agent_overrides(
            profile,
            agent,
            agent_profile=profiles.get(agent.profile_id),
        )
        harness_name = getattr(effective, "harness", None)
        harness = harness_registry.get(harness_name, project_id) if harness_name else None
        if harness is None:
            diagnostics.append(
                {
                    "agent_id": agent.id,
                    "code": "missing_harness",
                    "message": f"harness {harness_name!r} is unavailable",
                }
            )
            continue
        class_id = builder.resolve_class_id(effective)
        if not class_id or class_id not in builder._intelligence_classes:
            diagnostics.append(
                {
                    "agent_id": agent.id,
                    "code": "missing_class",
                    "message": f"intelligence class {class_id!r} is unavailable",
                }
            )
            continue
        settings = builder.resolve_launch_settings(effective, harness)
        if not settings["provider"]:
            diagnostics.append(
                {
                    "agent_id": agent.id,
                    "code": "missing_provider",
                    "message": "the harness has no resolvable provider",
                }
            )
            continue
        if not settings["model"]:
            diagnostics.append(
                {
                    "agent_id": agent.id,
                    "code": "missing_model",
                    "message": "the worker has no resolvable model",
                }
            )
            continue
        identity = ExecutionIdentity(
            profile_id=profile.id,
            harness=harness_name,
            provider=settings["provider"],
            model=settings["model"],
            intelligence_class=settings["intelligence_class"],
            reasoning_effort=settings["reasoning_effort"],
        )
        key = execution_type_key(identity)
        identities[key] = identity
        grouped.setdefault(key, []).append(agent.id)
        if (
            agent.state == AgentState.IDLE
            and agent.current_task_id is None
            and agent.id not in occupied
        ):
            idle[key] = idle.get(key, 0) + 1

    return ExecutionCatalog(
        types={key: identities[key] for key in sorted(identities)},
        members={key: tuple(sorted(grouped[key])) for key in sorted(grouped)},
        idle_counts={key: idle.get(key, 0) for key in sorted(grouped)},
        diagnostics=tuple(diagnostics),
    )
