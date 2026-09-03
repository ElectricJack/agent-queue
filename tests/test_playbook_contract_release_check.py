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
    profile_fingerprints_for,
    release_check,
    shipped_profile_fingerprints,
    shipped_profile_lookup,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = REPO_ROOT / REVIEWED_FIXTURE_ROOT

#: The command whose fingerprint the intentional-change fixture perturbs.
CHANGED_COMMAND = "ensure_task"
CHANGED_PLAYBOOK = "default-pipeline"
#: The delegated capability profile the pipeline hands per-task review to.  It
#: reaches the artifact only as an `ensure_task` argument — there is no AI step
#: in `default-pipeline` — which is why `solid-harbor.54` found it unrecorded.
CHANGED_PROFILE = "reviewer"


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


def test_changed_profile_capabilities_block_readiness() -> None:
    """A capability change to a *delegated* profile stales the artifact.

    The reviewer approved what `reviewer` was allowed to do, not merely that a
    `reviewer` directory exists.  Widening or narrowing it after approval must
    reach the gate, exactly as a changed command contract does.
    """
    perturbed = dict(shipped_profile_fingerprints())
    perturbed[CHANGED_PROFILE] = "sha256:" + "e" * 64
    report = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        profile_fingerprints=perturbed,
    )

    assert report["success"] is False
    row = next(r for r in report["stale"] if r["dependency"] == CHANGED_PROFILE)
    assert row["kind"] == "profile"
    assert row["origin"] == "fixture"
    assert row["change"] == "changed"
    assert row["playbook_id"] == CHANGED_PLAYBOOK
    assert row["reviewed_fingerprint"] == _artifact(CHANGED_PLAYBOOK)[
        "compiled_against"
    ]["profiles"][CHANGED_PROFILE]
    assert row["current_fingerprint"] == "sha256:" + "e" * 64
    assert CHANGED_PROFILE in row["message"]


def test_removed_profile_blocks_readiness() -> None:
    perturbed = {
        name: value
        for name, value in shipped_profile_fingerprints().items()
        if name != CHANGED_PROFILE
    }
    report = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        profile_fingerprints=perturbed,
    )
    assert report["success"] is False
    row = next(r for r in report["stale"] if r["dependency"] == CHANGED_PROFILE)
    assert row["kind"] == "profile"
    assert row["change"] == "removed"
    assert row["current_fingerprint"] is None


def test_the_profile_half_runs_without_being_asked_to() -> None:
    """The regression `solid-harbor.54` found: the caller passed nothing.

    `profile_fingerprints` used to default to "skip the profile comparison",
    and no caller — the command, the doctor check, or this suite — ever passed
    it.  The default is now the shipped profile set, so the drift above is
    reachable from `release_check(contract_registry=...)` alone.
    """
    assert release_check(contract_registry=CONTRACTS, fixture_root=FIXTURE_ROOT)[
        "success"
    ] is True
    assert shipped_profile_fingerprints()[CHANGED_PROFILE] == _artifact(CHANGED_PLAYBOOK)[
        "compiled_against"
    ]["profiles"][CHANGED_PROFILE]


def test_an_activation_is_held_to_its_own_live_profiles() -> None:
    """An activation row carries the fingerprints *its* daemon resolves.

    An activated artifact was compiled against the profiles in that database,
    operator edits included.  Holding it to `src/profiles/defaults/` would
    report a legitimately customised profile as drift, so a row's
    `current_profiles` wins over the shipped map when present.
    """
    artifact_profiles = {"house-reviewer": "sha256:" + "1" * 64}
    agreeing = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        activations=[
            {
                "playbook_id": "house-one",
                "enabled": True,
                "artifact_commands": {CHANGED_COMMAND: current_command_fingerprints(CONTRACTS)[CHANGED_COMMAND]},
                "artifact_profiles": artifact_profiles,
                "current_profiles": dict(artifact_profiles),
            }
        ],
    )
    assert agreeing["success"] is True, agreeing["stale"]
    assert "house-one" in agreeing["checked"]

    moved = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        activations=[
            {
                "playbook_id": "house-one",
                "enabled": True,
                "artifact_commands": {CHANGED_COMMAND: current_command_fingerprints(CONTRACTS)[CHANGED_COMMAND]},
                "artifact_profiles": artifact_profiles,
                "current_profiles": {"house-reviewer": "sha256:" + "2" * 64},
            }
        ],
    )
    assert moved["success"] is False
    row = next(r for r in moved["stale"] if r["playbook_id"] == "house-one")
    assert (row["origin"], row["kind"], row["dependency"]) == (
        "activation",
        "profile",
        "house-reviewer",
    )


def test_profile_fingerprints_for_omits_what_the_lookup_cannot_resolve() -> None:
    lookup = shipped_profile_lookup()
    resolved = profile_fingerprints_for(lookup, [CHANGED_PROFILE, "no-such-profile"])
    assert set(resolved) == {CHANGED_PROFILE}
    assert resolved[CHANGED_PROFILE] == shipped_profile_fingerprints()[CHANGED_PROFILE]


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
    """A stale artifact belonging to a disabled playbook is not a gate.

    The acknowledged row is disabled too: a waiver excuses an activation the
    operator actually took out of service, and nothing else (`sound-horizon-20`).
    """
    stale_commands = {CHANGED_COMMAND: "sha256:" + "d" * 64}
    rows = [
        {
            "playbook_id": "disabled-one",
            "enabled": False,
            "artifact_commands": stale_commands,
        },
        {
            "playbook_id": "acknowledged-one",
            "enabled": False,
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


def test_an_acknowledged_but_enabled_activation_still_blocks() -> None:
    """A waiver may not suppress the check for a playbook that is still live.

    `sound-horizon-20`: `_disposition` turns any acknowledged entry into
    `disabled` in the inventory, but the acknowledgement never touched the
    activation row.  An enabled activation carrying `acknowledged_by` was
    skipped here, so a waiver written for an intentionally disabled playbook
    silently certified stale artifacts that the daemon was really executing.
    """
    stale_commands = {CHANGED_COMMAND: "sha256:" + "d" * 64}
    report = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        activations=[
            {
                "playbook_id": "acknowledged-and-live",
                "enabled": True,
                "acknowledged_by": "operator",
                "artifact_commands": stale_commands,
            }
        ],
    )

    assert report["success"] is False
    assert "acknowledged-and-live" in report["checked"]
    assert [
        row
        for row in report["stale"]
        if row["playbook_id"] == "acknowledged-and-live"
        and row["dependency"] == CHANGED_COMMAND
    ]


def test_an_acknowledged_but_enabled_activation_without_evidence_is_unverified() -> None:
    """The same row with no artifact evidence is named, not skipped."""
    report = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        activations=[
            {
                "playbook_id": "acknowledged-and-live",
                "enabled": True,
                "acknowledged_by": "operator",
            }
        ],
    )

    assert report["success"] is False
    assert [
        row
        for row in report["unverified"]
        if row["playbook_id"] == "acknowledged-and-live"
        and row["reason"] == "no_command_evidence"
    ]


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


def test_every_shipped_fixture_is_checked() -> None:
    """All enabled shipped fixtures participate in the contract release gate."""
    from tests.playbook_fixture_activation import activatable_fixture_ids

    report = release_check(contract_registry=CONTRACTS, fixture_root=FIXTURE_ROOT)
    assert set(report["checked"]) == set(activatable_fixture_ids(FIXTURE_ROOT))
    assert "memory-consolidation" in report["checked"]


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


# ---------------------------------------------------------------------------
# Evidence the check could not read (prime-zenith-66)
# ---------------------------------------------------------------------------
#
# The gate's whole value is the claim "every enabled activation was compared".
# Before this section a live daemon whose activation query, artifact store or
# profile registry was unavailable produced exactly the same payload as a clean
# fleet: `success: True`, four shipped fixtures in `checked`, no stale rows.
# Absence of evidence is not evidence of a clean release, so each unread source
# and each uncomparable activation is now a *named* blocking result.


def test_an_enabled_activation_without_command_evidence_blocks() -> None:
    """The deterministic reproduction from the exit gate.

    An enabled activation whose artifact could not be loaded reached
    `release_check` as a row with no `artifact_commands`, and line 1584's
    `continue` dropped it without a trace.
    """
    report = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        activations=[{"playbook_id": "enabled-but-unreadable", "enabled": True}],
    )

    assert report["success"] is False
    assert report["stale"] == []
    row = next(
        r for r in report["unverified"] if r["playbook_id"] == "enabled-but-unreadable"
    )
    assert row["reason"] == "no_command_evidence"
    assert "enabled-but-unreadable" in row["message"]
    assert any("enabled-but-unreadable" in reason for reason in report["blocking_reasons"])
    # It was not compared, so it must not be claimed as compared.
    assert "enabled-but-unreadable" not in report["checked"]


def test_an_unverified_row_carries_the_identity_an_operator_needs() -> None:
    report = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        activations=[
            {
                "playbook_id": "project-one",
                "enabled": True,
                "scope": "project",
                "scope_identifier": "proj-7",
                "artifact_sha256": "sha256:" + "c" * 64,
                "evidence_reason": "artifact_unreadable",
                "evidence_detail": "OSError: [Errno 2] No such file",
            }
        ],
    )

    row = next(r for r in report["unverified"] if r["playbook_id"] == "project-one")
    assert row["reason"] == "artifact_unreadable"
    assert row["scope"] == "project"
    assert row["scope_identifier"] == "proj-7"
    assert row["artifact_sha256"] == "sha256:" + "c" * 64
    assert "OSError" in row["message"]


def test_a_decided_activation_without_evidence_still_does_not_block() -> None:
    """A disabled row is a decision, not unread evidence — waived or not."""
    report = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        activations=[
            {"playbook_id": "disabled-one", "enabled": False},
            {"playbook_id": "acked-one", "enabled": False, "acknowledged_by": "operator"},
        ],
    )

    assert report["success"] is True, report["blocking_reasons"]
    assert report["unverified"] == []


def test_an_unread_evidence_source_blocks_the_release_check() -> None:
    report = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        evidence_errors=[
            {"source": "activations", "error": "OperationalError: no such table"}
        ],
    )

    assert report["success"] is False
    assert report["evidence_errors"] == [
        {"source": "activations", "error": "OperationalError: no such table"}
    ]
    assert any(
        "activations" in reason and "no such table" in reason
        for reason in report["blocking_reasons"]
    )


def test_an_unread_profile_registry_does_not_fall_back_to_the_shipped_profiles() -> None:
    """The live registry is the baseline; the shipped defaults are a different fact.

    Falling back silently held an operator-customised profile to
    `src/profiles/defaults/`, which either invents drift or — when the customised
    policy happens to agree — certifies a comparison that never happened.
    """
    profile = next(iter(shipped_profile_fingerprints()))
    live = current_command_fingerprints(CONTRACTS)
    report = release_check(
        contract_registry=CONTRACTS,
        fixture_root=FIXTURE_ROOT,
        activations=[
            {
                "playbook_id": "live-one",
                "enabled": True,
                # The command half is comparable and clean, so anything the
                # report blocks on comes from the profile half alone.
                "artifact_commands": {CHANGED_COMMAND: live[CHANGED_COMMAND]},
                "artifact_profiles": {profile: "sha256:" + "f" * 64},
                "current_profiles_unavailable": True,
            }
        ],
    )

    assert report["success"] is False
    # No profile drift is invented against a baseline that was never read.
    assert [row for row in report["stale"] if row["playbook_id"] == "live-one"] == []
    row = next(r for r in report["unverified"] if r["playbook_id"] == "live-one")
    assert row["reason"] == "profile_registry_unavailable"
    # The command half *was* compared, and the report says so.
    assert "live-one" in report["checked"]


def test_a_clean_tree_reports_no_unread_evidence() -> None:
    report = release_check(contract_registry=CONTRACTS, fixture_root=FIXTURE_ROOT)
    assert report["success"] is True
    assert report["evidence_errors"] == []
    assert report["unverified"] == []
    assert report["blocking_reasons"] == []
