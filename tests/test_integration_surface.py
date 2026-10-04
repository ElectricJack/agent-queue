"""The consolidated integration operator surface (simplification §5.1, Phase 4).

``aq integration`` shows four human decisions, two diagnostics and ``flush``;
every other control is a gated legacy exception listed, with its replacement
and removal gate, in :mod:`src.commands.integration_legacy`.  These tests keep
the CLI, the contract registry, scope and the supervisor's grants in step with
that table.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from src.api.scope import (
    LOCAL_INTEGRATION_CONTROLS,
    OPERATOR_INTEGRATION_CONTROLS,
    RequestScope,
    check_command_scope,
)
from src.commands.contracts.builtin import register_builtin_contracts
from src.commands.contracts.registry import ContractRegistry
from src.commands.integration_legacy import (
    AGENT_PROTOCOL_CONTROLS,
    APPROVED_INTEGRATION_CONTROLS,
    APPROVED_INTEGRATION_DOCTOR_CHECKS,
    LEGACY_INTEGRATION_CONTROLS,
    LEGACY_INTEGRATION_DOCTOR_CHECKS,
    PLAYBOOK_INTEGRATION_COMMANDS,
    legacy_control,
)
from src.profiles.capabilities import CapabilityPolicy
from src.profiles.parser import parse_profile
from src.vault import ensure_default_profiles


def _command(path: str) -> click.Command | None:
    from src.cli.app import cli

    node: click.Command | None = cli
    ctx = click.Context(cli)
    for name in path.split():
        if not isinstance(node, click.Group):
            return None
        node = node.get_command(ctx, name)
        if node is None:
            return None
    return node


def _integration_group() -> click.Group:
    group = _command("integration")
    assert isinstance(group, click.Group)
    return group


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    registry = ContractRegistry()
    register_builtin_contracts(registry)
    return registry


@pytest.fixture(scope="module")
def supervisor(tmp_path_factory) -> CapabilityPolicy:
    root = tmp_path_factory.mktemp("vault")
    ensure_default_profiles(str(root))
    path = Path(root) / "vault" / "agent-types" / "supervisor" / "profile.md"
    parsed = parse_profile(path.read_text(encoding="utf-8"))
    assert parsed.capabilities is not None
    return CapabilityPolicy.from_namespaces(**parsed.capabilities)


def test_operator_surface_is_four_decisions_two_diagnostics_and_flush():
    kinds = [control.kind for control in APPROVED_INTEGRATION_CONTROLS]
    assert kinds.count("decision") == 4
    assert kinds.count("diagnostic") == 2
    assert [c.path for c in APPROVED_INTEGRATION_CONTROLS if c.kind == "hand"] == [
        "integration flush"
    ]


def test_every_visible_integration_command_is_approved_or_an_agent_protocol_step():
    visible = {
        f"integration {name}"
        for name, command in _integration_group().commands.items()
        if not command.hidden and name != "legacy"
    }
    approved = {c.path.split()[1] for c in APPROVED_INTEGRATION_CONTROLS}
    allowed = {f"integration {name}" for name in approved} | {
        c.path for c in AGENT_PROTOCOL_CONTROLS
    }
    assert visible == allowed


def test_approved_controls_resolve_to_their_contracts(registry):
    for control in APPROVED_INTEGRATION_CONTROLS + AGENT_PROTOCOL_CONTROLS:
        assert _command(control.path) is not None, control.path
        assert registry.get(control.command_id) is not None, control.command_id


def test_every_legacy_command_is_registered_with_a_replacement_and_removal_gate():
    legacy = _integration_group().commands["legacy"]
    assert isinstance(legacy, click.Group)
    for name in legacy.commands:
        control = legacy_control(f"integration legacy {name}")
        assert control is not None, f"aq integration legacy {name} is not in the registry"
    for control in LEGACY_INTEGRATION_CONTROLS:
        assert _command(control.path) is not None, control.path
        assert control.replacement.strip(), control.path
        assert control.removal_gate.strip(), control.path


def test_old_flat_paths_resolve_to_the_legacy_command():
    for control in LEGACY_INTEGRATION_CONTROLS:
        words = control.path.split()
        if words[:2] != ["integration", "legacy"]:
            continue
        flat = _command(f"integration {words[2]}")
        assert flat is _command(control.path), control.path


def test_every_integration_contract_is_approved_legacy_protocol_or_a_playbook_step(registry):
    classified = {
        control.command_id
        for control in (
            APPROVED_INTEGRATION_CONTROLS + AGENT_PROTOCOL_CONTROLS + LEGACY_INTEGRATION_CONTROLS
        )
        if control.command_id
    } | PLAYBOOK_INTEGRATION_COMMANDS
    unclassified = sorted(
        name for name in registry.names()
        if name.startswith("integration_") and name not in classified
    )
    assert unclassified == []
    operator = {c.command_id for c in APPROVED_INTEGRATION_CONTROLS + LEGACY_INTEGRATION_CONTROLS}
    assert not operator & PLAYBOOK_INTEGRATION_COMMANDS


def test_supervisor_grants_and_scope_agree_with_the_registry(supervisor):
    elevated = RequestScope(kind="session", session_id="sup", project_id="p1", elevated=True)
    for control in APPROVED_INTEGRATION_CONTROLS + LEGACY_INTEGRATION_CONTROLS:
        command = control.command_id
        if command is None:
            continue
        integration = command.startswith("integration_") or command == "task_deliver"
        # Every approved control is granted; gate answer binds only a person's
        # answer, which the handler (not scope) enforces.
        if getattr(control, "supervisor", True):
            assert supervisor.allows_aq_command(command), command
            if integration:
                assert check_command_scope(command, {}, elevated) is None, command
                # ``status`` stays a project read; every write is authority-gated.
                if control.kind != "diagnostic":
                    assert command in OPERATOR_INTEGRATION_CONTROLS, command
        else:
            assert not supervisor.allows_aq_command(command), command


def test_local_only_recovery_is_refused_before_its_handler():
    """Simplification Appendix B.8.2: scope no longer admits what the service refuses."""
    elevated = RequestScope(kind="session", session_id="sup", project_id="p1", elevated=True)
    for command in LOCAL_INTEGRATION_CONTROLS:
        assert command not in OPERATOR_INTEGRATION_CONTROLS
        assert "local operator" in check_command_scope(command, {}, elevated)
        assert check_command_scope(command, {}, RequestScope(kind="local")) is None


def test_record_noop_is_a_supervisor_control():
    """Simplification Appendix B.8.1: the profile directs the supervisor to it."""
    from src.commands.integration_commands import _SUPERVISOR_REDRIVE_CAPABILITIES

    assert "integration_record_noop" in _SUPERVISOR_REDRIVE_CAPABILITIES
    assert legacy_control("integration legacy record-noop").supervisor


def test_integration_help_points_at_the_legacy_group():
    from src.cli.app import cli

    result = CliRunner().invoke(cli, ["integration", "--help"])
    assert result.exit_code == 0, result.output
    assert "aq integration legacy" in result.output
    for name in ("enable", "redrive-root", "record-noop", "develop"):
        assert f"  {name} " not in result.output


def test_doctor_has_three_subject_checks_and_gates_every_older_one():
    from src.doctor import default_registry

    registered = {check_id for check_id in default_registry().ids() if check_id.startswith(
        "integration.")}
    legacy = {entry.check_id for entry in LEGACY_INTEGRATION_DOCTOR_CHECKS}
    assert len(APPROVED_INTEGRATION_DOCTOR_CHECKS) == 3
    assert len(legacy) == len(LEGACY_INTEGRATION_DOCTOR_CHECKS) == 21
    assert not legacy & set(APPROVED_INTEGRATION_DOCTOR_CHECKS)
    assert registered == legacy | set(APPROVED_INTEGRATION_DOCTOR_CHECKS)
    for entry in LEGACY_INTEGRATION_DOCTOR_CHECKS:
        assert entry.replacement and entry.removal_gate, entry
        if entry.replacement.startswith("integration."):
            assert entry.replacement.split()[0] in APPROVED_INTEGRATION_DOCTOR_CHECKS
