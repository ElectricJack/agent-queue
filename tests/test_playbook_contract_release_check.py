"""The release gate: a changed command contract must ship rebuilt artifacts.

Package 6 §5.5 (T-13).  This suite runs in the existing `default` CI matrix,
which is the only place a contract change and the reviewed fixtures are both
present — so no new CI job is needed.  `aq doctor --check
playbooks.stale_artifacts` asks the same question of a live daemon.

The check is deliberately **offline**: it compares checked-in fixtures against
the in-process registry.  It never recompiles, because compilation is LLM-driven
and a CI job that regenerated the artifact would launder an unreviewed change
into the approved recording.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.commands.contracts import CONTRACTS
from src.playbooks.migration import (
    REVIEWED_FIXTURE_ROOT,
    current_command_fingerprints,
    release_check,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = REPO_ROOT / REVIEWED_FIXTURE_ROOT

#: The command whose fingerprint the intentional-change fixture perturbs.
CHANGED_COMMAND = "ensure_task"
CHANGED_PLAYBOOK = "default-pipeline"


class _StubRegistration:
    def __init__(self, contract):
        self.contract = contract


class _PerturbedContract:
    """A contract whose *execution* fingerprint differs by one character."""

    def __init__(self, real, fingerprint: str) -> None:
        self._real = real
        self._fingerprint = fingerprint

    def fingerprint(self) -> str:
        return self._fingerprint

    def __getattr__(self, item):
        return getattr(self._real, item)


class _PerturbedRegistry:
    """`CONTRACTS` with exactly one command's execution fingerprint moved."""

    def __init__(self, command: str, fingerprint: str | None) -> None:
        self._command = command
        self._fingerprint = fingerprint

    def names(self):
        if self._fingerprint is None:
            return frozenset(CONTRACTS.names()) - {self._command}
        return CONTRACTS.names()

    def get(self, name: str):
        registration = CONTRACTS.get(name)
        if name != self._command or registration is None or self._fingerprint is None:
            return registration
        return _StubRegistration(_PerturbedContract(registration.contract, self._fingerprint))

    def registry_fingerprint(self) -> str:
        return "sha256:" + "1" * 64


def _artifact(playbook_id: str) -> dict:
    return json.loads(
        (FIXTURE_ROOT / playbook_id / "artifact.json").read_text(encoding="utf-8")
    )


def test_clean_tree_passes() -> None:
    report = release_check(contract_registry=CONTRACTS, fixture_root=FIXTURE_ROOT)
    assert report["success"] is True, report["stale"]
    assert report["stale"] == []
    assert CHANGED_PLAYBOOK in report["checked"]


def test_changed_fingerprint_blocks_readiness() -> None:
    """The intentional contract-change fixture the roadmap requires."""
    registry = _PerturbedRegistry(CHANGED_COMMAND, "sha256:" + "a" * 64)
    report = release_check(contract_registry=registry, fixture_root=FIXTURE_ROOT)

    assert report["success"] is False
    named = {(row["playbook_id"], row["dependency"]) for row in report["stale"]}
    assert (CHANGED_PLAYBOOK, CHANGED_COMMAND) in named
    row = next(
        r for r in report["stale"] if r["dependency"] == CHANGED_COMMAND
    )
    assert row["change"] == "changed"
    assert row["origin"] == "fixture"
    assert row["reviewed_fingerprint"] == _artifact(CHANGED_PLAYBOOK)[
        "compiled_against"
    ]["commands"][CHANGED_COMMAND]
    assert row["current_fingerprint"] == "sha256:" + "a" * 64
    assert CHANGED_COMMAND in row["message"] and CHANGED_PLAYBOOK in row["message"]


def test_removed_command_blocks_readiness() -> None:
    registry = _PerturbedRegistry(CHANGED_COMMAND, None)
    report = release_check(contract_registry=registry, fixture_root=FIXTURE_ROOT)
    assert report["success"] is False
    row = next(r for r in report["stale"] if r["dependency"] == CHANGED_COMMAND)
    assert row["change"] == "removed"
    assert row["current_fingerprint"] is None


def test_a_stale_artifact_is_stale_contract_for_the_activation_health_surface() -> None:
    """The same mismatch the release check names marks an activation `stale_contract`.

    This is the half of §5.5 assertion 2 that says a new run is refused: the
    engine refuses to start a run on an activation whose health is not `ready`,
    and `evaluate_health` is what puts it in `stale_contract`.
    """
    from src.playbooks.activation import ActivationHealth, evaluate_health
    from src.playbooks.artifact_ref import ArtifactRef

    artifact = _artifact(CHANGED_PLAYBOOK)
    reviewed = artifact["compiled_against"]["commands"]
    current = dict(reviewed)
    current[CHANGED_COMMAND] = "sha256:" + "a" * 64

    health, reasons = evaluate_health(
        enabled=True,
        artifact=ArtifactRef(
            playbook_id=CHANGED_PLAYBOOK,
            artifact_sha256="sha256:" + "b" * 64,
            schema_generation=2,
            contract_fingerprint="sha256:" + "c" * 64,
            source_digest=artifact["source_hash"],
            compiler_build=artifact["compiler_build"],
            version=1,
        ),
        artifact_present=True,
        validation={"errors": []},
        current_contract_fingerprints=current,
        artifact_contract_fingerprints=reviewed,
    )
    assert health is ActivationHealth.STALE_CONTRACT
    assert any(reason.subject == CHANGED_COMMAND for reason in reasons)


def test_presentation_change_does_not_trip_it() -> None:
    """Roadmap §4: presentation-only labels do not affect the execution fingerprint."""
    registration = CONTRACTS.get(CHANGED_COMMAND)
    assert registration is not None
    contract = registration.contract
    before = contract.fingerprint()

    relabelled = contract.model_copy(
        update={
            "presentation": contract.presentation.model_copy(
                update={
                    "title": "Ensure a task exists (renamed for the humans)",
                    "summary": "A different sentence entirely.",
                }
            )
        }
    )
    assert relabelled.presentation.title != contract.presentation.title
    assert relabelled.fingerprint() == before

    registry = _PerturbedRegistry(CHANGED_COMMAND, before)
    assert release_check(contract_registry=registry, fixture_root=FIXTURE_ROOT)["success"]


def test_disabled_playbooks_do_not_block() -> None:
    """A stale artifact belonging to a disabled or acknowledged playbook is not a gate."""
    stale_commands = {CHANGED_COMMAND: "sha256:" + "d" * 64}
    rows = [
        {
            "playbook_id": "disabled-one",
            "enabled": False,
            "artifact_commands": stale_commands,
        },
        {
            "playbook_id": "acknowledged-one",
            "enabled": True,
            "acknowledged_by": "operator",
            "artifact_commands": stale_commands,
        },
    ]
    report = release_check(
        contract_registry=CONTRACTS, fixture_root=FIXTURE_ROOT, activations=rows
    )
    assert report["success"] is True, report["stale"]
    assert "disabled-one" not in report["checked"]
    assert "acknowledged-one" not in report["checked"]


def test_an_enabled_stale_activation_does_block() -> None:
    report = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        activations=[
            {
                "playbook_id": "enabled-one",
                "enabled": True,
                "artifact_commands": {CHANGED_COMMAND: "sha256:" + "d" * 64},
            }
        ],
    )
    assert report["success"] is False
    row = next(r for r in report["stale"] if r["playbook_id"] == "enabled-one")
    assert row["origin"] == "activation"


def test_rejected_fixtures_are_not_checked() -> None:
    """A recorded negative is not held to the live contract surface.

    Reporting drift in a playbook nothing may run would make the gate noisy in
    exactly the case where a human has already written down what is wrong.
    """
    from tests.playbook_fixture_activation import activatable_fixture_ids

    report = release_check(contract_registry=CONTRACTS, fixture_root=FIXTURE_ROOT)
    assert set(report["checked"]) == set(activatable_fixture_ids(FIXTURE_ROOT))
    assert "memory-consolidation" not in report["checked"]


def test_check_is_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network and no LLM: it compares files against the in-process registry."""
    import socket

    def _forbidden(*args, **kwargs):  # pragma: no cover - the point is that it never runs
        raise AssertionError("release_check opened a socket")

    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    from src.playbooks import proposal

    def _no_compile(*args, **kwargs):  # pragma: no cover - same
        raise AssertionError("release_check compiled a proposal")

    monkeypatch.setattr(proposal, "propose", _no_compile)

    assert release_check(contract_registry=CONTRACTS, fixture_root=FIXTURE_ROOT)["success"]


def test_default_fixture_root_is_the_checked_in_one() -> None:
    """The default argument must point at the fixtures, not at a temp directory."""
    assert (REPO_ROOT / REVIEWED_FIXTURE_ROOT).is_dir()
    assert release_check(contract_registry=CONTRACTS)["checked"]


def test_current_command_fingerprints_covers_the_whole_registry() -> None:
    fingerprints = current_command_fingerprints(CONTRACTS)
    assert set(fingerprints) == set(CONTRACTS.names())
    assert all(value.startswith("sha256:") for value in fingerprints.values())


def test_doctor_registers_the_release_check() -> None:
    """An unregistered check is a check that never runs (commit 10f2b2d2)."""
    from src.doctor import default_registry
    from src.doctor.playbook_v2_checks import RELEASE_CHECK_ID

    registry = default_registry()
    ids = {check.id for check in registry.checks()}
    assert RELEASE_CHECK_ID in ids


@pytest.mark.asyncio
async def test_doctor_check_reports_ok_on_a_clean_tree() -> None:
    from src.config import AppConfig
    from src.doctor.models import DoctorContext, Severity
    from src.doctor.playbook_v2_checks import RELEASE_CHECK_ID, _check_stale_artifacts

    result = await _check_stale_artifacts(DoctorContext(config=AppConfig()))
    assert result.id == RELEASE_CHECK_ID
    assert result.severity is Severity.OK
    assert CHANGED_PLAYBOOK in result.data["checked"]
