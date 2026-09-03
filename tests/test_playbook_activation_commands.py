from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.commands.playbook_v2_commands import PlaybookV2CommandsMixin
from src.config import PlaybooksConfig
from src.playbooks.activation import ActivationHealth, ActivationHealthRecord
from src.playbooks.validation import RegisteredEventLookup, RegistryContractLookup
from tests.playbook_v2_helpers import StubProfiles
from tests.test_api_playbook_v2_commands import _backend_fixture


class _Handler(PlaybookV2CommandsMixin):
    def __init__(self, definition, ref, records):
        self.definition = definition
        self.ref = ref
        self.records = list(records)
        self.config = SimpleNamespace(
            playbooks=PlaybooksConfig(
                v2_api=True,
                v2_storage_enabled=True,
                v2_activation_writes=True,
            )
        )
        self.db = SimpleNamespace(
            list_playbook_activations=AsyncMock(),
            get_playbook_artifact=AsyncMock(),
            get_playbook_artifact_row=AsyncMock(),
            set_playbook_activation=AsyncMock(),
        )

    async def _v2_load_artifact(self, sha, playbook_id=None):
        return self.ref, self.definition, None

    async def _v2_health_records(self):
        records = self.records.pop(0) if self.records else []
        return records, RegistryContractLookup(), StubProfiles()

    async def _v2_lookups(self):
        return RegistryContractLookup(), StubProfiles(), RegisteredEventLookup()


def _record(ref, *, actor="operator"):
    return ActivationHealthRecord(
        "activation-1",
        ref.playbook_id,
        "system",
        "",
        True,
        ref.artifact_sha256,
        ActivationHealth.READY,
        (),
        activated_by=actor,
    )


async def test_activate_requires_acknowledge_diff_for_executable_change():
    definition, ref, _activation = _backend_fixture()
    handler = _Handler(definition, ref, [[]])
    result = await handler._cmd_playbook_activate(
        {"playbook_id": definition.id, "artifact_sha256": ref.artifact_sha256}
    )
    assert result["blocked"] is True
    assert result["changed"] is False
    handler.db.set_playbook_activation.assert_not_awaited()


async def test_activate_records_activated_by():
    definition, ref, _activation = _backend_fixture()
    handler = _Handler(definition, ref, [[], [_record(ref, actor="local")]])
    result = await handler._cmd_playbook_activate(
        {
            "playbook_id": definition.id,
            "artifact_sha256": ref.artifact_sha256,
            "acknowledge_diff": ref.artifact_sha256,
            "reviewed_by": "spoofed-reviewer",
        }
    )
    assert result["blocked"] is False
    assert handler.db.set_playbook_activation.await_args.kwargs["activated_by"] == "local"


async def test_project_activation_records_review_for_the_exact_artifact():
    """A project activation is the durable, attributable human review decision."""
    from src.playbooks.definition import ProjectScope

    definition, ref, _activation = _backend_fixture()
    definition = definition.model_copy(update={"scope": ProjectScope(project_id="project-a")})
    handler = _Handler(definition, ref, [[], [_record(ref, actor="local")]])

    result = await handler._cmd_playbook_activate(
        {
            "playbook_id": definition.id,
            "artifact_sha256": ref.artifact_sha256,
            "acknowledge_diff": ref.artifact_sha256,
        }
    )

    assert result["blocked"] is False
    write = handler.db.set_playbook_activation.await_args.kwargs
    assert write["scope"] == "project"
    assert write["scope_identifier"] == "project-a"
    assert write["reviewed_artifact_sha256"] == ref.artifact_sha256
    assert write["reviewed_by"] == "local"


async def test_project_activation_refuses_a_different_project_principal():
    """A project-scoped supervisor cannot approve another project's artifact."""
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.playbooks.definition import ProjectScope
    from src.profiles.capabilities import DENY_ALL

    definition, ref, _activation = _backend_fixture()
    definition = definition.model_copy(update={"scope": ProjectScope(project_id="project-b")})
    handler = _Handler(definition, ref, [[]])
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="supervisor-project-a",
        project_id="project-a",
        elevated=True,
    )

    with principal_context(principal):
        result = await handler._cmd_playbook_activate(
            {
                "playbook_id": definition.id,
                "artifact_sha256": ref.artifact_sha256,
                "acknowledge_diff": ref.artifact_sha256,
                "project_id": "project-a",
            }
        )

    assert result == {"error": "out of scope: project_id mismatch"}
    handler.db.set_playbook_activation.assert_not_awaited()


async def test_activate_refuses_invalid_artifact():
    definition, ref, _activation = _backend_fixture()
    rule = definition.rules[0].model_copy(
        update={"trigger": definition.rules[0].trigger.model_copy(update={"event_type": "unknown.event"})}
    )
    invalid = definition.model_copy(update={"rules": [rule, *definition.rules[1:]]})
    handler = _Handler(invalid, ref, [[]])
    result = await handler._cmd_playbook_activate(
        {
            "playbook_id": definition.id,
            "artifact_sha256": ref.artifact_sha256,
            "acknowledge_diff": ref.artifact_sha256,
        }
    )
    assert result["blocked"] is True
    assert any("event" in blocker.lower() for blocker in result["blockers"])


async def test_activate_synthesises_an_activation_when_the_health_read_misses_the_row():
    """The write committed, so the response describes it rather than crashing.

    ``load_activation_health`` can come back without the row that was just
    written — the artifact became unreadable between the write and the
    re-read, or the read does not see it yet.  The success path used to
    dereference that ``None``; it now mirrors the blocked path and synthesises
    the payload, with the health it could not verify reported as
    ``unavailable``.
    """
    definition, ref, _activation = _backend_fixture()
    handler = _Handler(definition, ref, [[], []])

    result = await handler._cmd_playbook_activate(
        {
            "playbook_id": definition.id,
            "artifact_sha256": ref.artifact_sha256,
            "acknowledge_diff": ref.artifact_sha256,
        }
    )

    assert result["success"] is True
    assert result["blocked"] is False
    handler.db.set_playbook_activation.assert_awaited_once()
    activation = result["activation"]
    assert activation["playbook_id"] == definition.id
    assert activation["active_artifact_sha256"] == ref.artifact_sha256
    assert activation["enabled"] is True
    assert activation["health"] == "unavailable"
    assert activation["scope"] == definition.scope.type
    [reason] = activation["reasons"]
    assert reason["code"] == "activation_health_unreadable"
    assert definition.id in reason["message"]
    assert ref.artifact_sha256 in reason["message"]
