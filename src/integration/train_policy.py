"""Read the project's current train playbook without creating a subject or journal.

An existing project without a train policy keeps its legacy behavior until it
installs the default. Once bound, disabling that activation is the train switch.
Artifact failures are finite retry decisions; they never authorize publication.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from src.database.tables import playbook_activations, playbook_artifacts, projects
from src.playbooks.train_policy import TrainPolicy


@dataclass(frozen=True)
class TrainPolicyView:
    enabled: bool = True
    policy: TrainPolicy | None = None
    reason: str | None = None


def artifact_policy(digest, path):
    from src.playbooks.definition import load_definition_json

    data = Path(path).read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("train artifact digest mismatch")
    policy = load_definition_json(data.decode()).integration_policy
    return policy.train if policy else None


async def train_policy_on(conn, project_id: str) -> TrainPolicyView:
    configured = await conn.scalar(select(projects.c.hierarchical_integration_policy).where(
        projects.c.id == project_id))
    binding = ((configured or {}).get("train") or {}).get("playbook_id")
    selected = binding or "fast-smooth-train"
    rows = (await conn.execute(select(
        playbook_activations.c.playbook_id, playbook_activations.c.enabled,
        playbook_activations.c.health, playbook_artifacts.c.artifact_sha256,
        playbook_artifacts.c.path,
    ).outerjoin(playbook_artifacts, playbook_artifacts.c.artifact_sha256
                == playbook_activations.c.active_artifact_sha256).where(
        playbook_activations.c.playbook_id == selected,
        playbook_activations.c.scope == "project",
        playbook_activations.c.scope_identifier == project_id,
    ))).mappings().all()
    found = []
    for row in rows:
        try:
            policy = await asyncio.to_thread(
                artifact_policy, row["artifact_sha256"], row["path"],
            )
        except (OSError, ValueError, TypeError):
            return TrainPolicyView(False, reason="train_policy_unavailable")
        if policy is not None:
            found.append((row, policy))
    if not found:
        return (TrainPolicyView(False, reason="train_policy_unavailable")
                if binding or rows else TrainPolicyView())
    if len(found) != 1:
        return TrainPolicyView(False, reason="train_policy_ambiguous")
    row, policy = found[0]
    if not row["enabled"]:
        return TrainPolicyView(False, policy, "train_playbook_disabled")
    if row["health"] != "ready":
        return TrainPolicyView(False, policy, "train_policy_unavailable")
    return TrainPolicyView(True, policy)


async def load_train_policy(db, project_id: str) -> TrainPolicyView:
    async with db._engine.connect() as conn:
        return await train_policy_on(conn, project_id)


def current_checks(policy: dict, view: TrainPolicyView) -> dict:
    """Replace check policy only; frozen source identity and intent are untouched."""
    if view.policy is None or view.policy.required_checks is None:
        return policy
    return {**policy, "root": {**policy.get("root", {}),
            "required_checks": view.policy.required_checks.model_dump(mode="json")}}
