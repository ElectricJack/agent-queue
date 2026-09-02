"""Explicit V2 activation health, intentionally independent of enablement."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.playbooks.artifact_ref import ArtifactRef


class ActivationHealth(str, Enum):
    READY = "ready"
    QUESTION_REQUIRED = "question_required"
    INVALID = "invalid"
    DISABLED = "disabled"
    STALE_CONTRACT = "stale_contract"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class HealthReason:
    code: str
    message: str
    subject: str | None = None
    expected_fingerprint: str | None = None
    actual_fingerprint: str | None = None


def evaluate_health(
    *,
    enabled: bool,
    artifact: ArtifactRef | None,
    artifact_present: bool,
    validation: Mapping[str, Any],
    current_contract_fingerprints: Mapping[str, str],
    artifact_contract_fingerprints: Mapping[str, str],
) -> tuple[ActivationHealth, tuple[HealthReason, ...]]:
    if artifact is None or not artifact_present:
        return ActivationHealth.UNAVAILABLE, (HealthReason("artifact_missing", "Artifact is unavailable"),)
    errors = validation.get("errors", [])
    if errors:
        return ActivationHealth.INVALID, tuple(
            HealthReason("validation_failed", str(error), getattr(error, "step_id", None))
            for error in errors
        )
    questions = validation.get("questions", [])
    if questions:
        return ActivationHealth.QUESTION_REQUIRED, tuple(
            HealthReason("question_required", str(question), getattr(question, "step_id", None))
            for question in questions
        )
    stale: list[HealthReason] = []
    for command, expected in artifact_contract_fingerprints.items():
        actual = current_contract_fingerprints.get(command)
        if actual is None:
            stale.append(HealthReason("command_removed", "Command is no longer registered", command, expected, None))
        elif actual != expected:
            stale.append(HealthReason("command_contract_changed", "Command contract changed", command, expected, actual))
    if stale:
        return ActivationHealth.STALE_CONTRACT, tuple(stale)
    if not enabled:
        return ActivationHealth.DISABLED, ()
    return ActivationHealth.READY, ()
