"""Immutable values exchanged by triage services."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingChoice:
    task_id: str
    execution_type_key: str
    expected_revision: int
    reason: str


@dataclass(frozen=True)
class TriagePrincipal:
    project_id: str
    run_id: str
    session_id: str
    instance_token: str
