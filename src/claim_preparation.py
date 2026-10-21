"""Identity carried by a deferred claim-preparation response."""

from dataclasses import dataclass

from src.models import SessionRecord


def same_session_launch(expected: SessionRecord, observed: SessionRecord) -> bool:
    """A reused id/path is insufficient proof of the original worker launch."""
    identity = ("id", "instance_token", "started_at", "project_id", "profile_id",
                "agent_id", "work_dir", "lifecycle", "name", "harness", "provider")
    return all(getattr(expected, key) == getattr(observed, key) for key in identity)


@dataclass(frozen=True)
class CompletedClaimPreparation:
    session: SessionRecord
    task_id: str
    claim_epoch: int
    task_created_at: float
    preparation_started_at: float | None
    outcome: dict
