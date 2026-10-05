"""Read the immutable aggregate subject issued before a verifier is dispatched."""

from __future__ import annotations

from sqlalchemy import select

from src.database.tables import integration_outbox, integration_parent_episodes
from src.git.manager import is_valid_git_oid


def exact_red_parent_evidence(evidence: dict, *, operation: dict, checkpoint: dict) -> bool:
    """Only server-recorded conclusive failures of the frozen required checks count."""
    required = operation["policy_snapshot"]["parent"]["required_checks"]
    checks = evidence.get("checks") or {}
    return (
        evidence.get("operation_id") == operation["id"]
        and evidence.get("parent_task_id") == operation["parent_task_id"]
        and evidence.get("parent_generation") == checkpoint["generation"]
        and evidence.get("parent_head_sha") == checkpoint["checkpoint_sha"]
        and evidence.get("producer_id") == required["producer_id"]
        and evidence.get("required_check_version") == required["version"]
        and evidence.get("required_check_version") == operation["required_check_version"]
        and evidence.get("conclusion") == "failure"
        and evidence.get("classification") == "conclusive"
        and isinstance(checks, dict)
        and any(checks.get(name) in ("failure", "missing", "cancelled", "skipped", "neutral")
                for name in required["names"])
    )


def latest_red_parent_evidence(evidence, *, operation, checkpoint):
    """A required check whose latest observation is red keeps the head red.

    Evidence is grouped by ``workflow_id`` (suite identity): a re-observation of
    the same suite supersedes its prior red, but a sibling suite observed at a
    slightly different ``observed_at`` never does, so red is detected whenever
    required checks span multiple suites.
    """
    if not evidence:
        return None
    latest_by_suite: dict[str, dict] = {}
    for row in sorted(evidence, key=lambda row: (row["observed_at"], row["id"])):
        latest_by_suite[row["workflow_id"]] = row
    reds = [
        row
        for row in latest_by_suite.values()
        if exact_red_parent_evidence(row, operation=operation, checkpoint=checkpoint)
    ]
    return max(reds, key=lambda row: (row["observed_at"], row["id"])) if reds else None


async def verifier_subject_on(
    conn, *, verifier_id: str, project_id: str, operation: dict, checkpoint: dict,
    lock: bool = False,
) -> dict | None:
    """Return one exact handoff subject; never infer one from task/summary prose.

    Outbox delivery counters change, but the event identity and payload are frozen.
    Multiple issuances for one verifier are ambiguous even if one matches today.
    """
    statement = select(integration_outbox).where(
        integration_outbox.c.event_type == "task.integration_ready",
        integration_outbox.c.project_id == project_id,
        integration_outbox.c.payload["verifier_task_id"].as_string() == verifier_id,
    )
    if lock:
        statement = statement.with_for_update()
    rows = (await conn.execute(statement)).mappings().all()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("multiple immutable subjects name this aggregate verifier")
    row = rows[0]
    subject = row["payload"]
    episode = (
        await conn.execute(
            select(integration_parent_episodes).where(
                integration_parent_episodes.c.id == operation["episode_id"],
                integration_parent_episodes.c.parent_task_id == operation["parent_task_id"],
                integration_parent_episodes.c.repository_id == checkpoint["repository_id"],
            )
        )
    ).mappings().one_or_none()
    expected = {
        "project_id": project_id,
        "operation_id": operation["id"],
        "task_id": operation["parent_task_id"],
        "episode_id": operation["episode_id"],
        "verifier_task_id": verifier_id,
        "next_owner_id": verifier_id,
        "next_role": "verifier",
        "target": {
            "repository_id": checkpoint["repository_id"],
            "branch": checkpoint["branch"],
        },
        "head_sha": checkpoint["checkpoint_sha"],
    }
    generation = subject.get("generation")
    token = subject.get("expected_token")
    if (
        episode is None
        or checkpoint["task_id"] != operation["parent_task_id"]
        or checkpoint["episode_id"] != operation["episode_id"]
        or any(subject.get(key) != value for key, value in expected.items())
        or type(generation) is not int
        or not episode["generation"] <= generation <= checkpoint["generation"]
        or row["created_at"] < episode["created_at"]
        or type(token) is not int
        or token < 0
        or not is_valid_git_oid(subject.get("head_sha"))
    ):
        raise ValueError("immutable aggregate subject does not match this checkpoint")
    return {"outbox_id": row["id"], "created_at": row["created_at"], "subject": subject}
