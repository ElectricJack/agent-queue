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
            playbooks=PlaybooksConfig(enabled=True)
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


# -- set_playbook_enabled: pause / resume the activation in place -----------


def _activation_row(ref, *, enabled=True, sha=None):
    return {
        "activation_id": "activation-1",
        "playbook_id": ref.playbook_id,
        "scope": "system",
        "scope_identifier": "",
        "enabled": enabled,
        "active_artifact_sha256": ref.artifact_sha256 if sha is None else sha,
        "health": "ready" if enabled else "disabled",
        "reasons": "[]",
        "activated_by": "operator",
    }


async def test_set_playbook_enabled_pauses_the_active_artifact_in_place():
    definition, ref, _activation = _backend_fixture()
    handler = _Handler(definition, ref, [])
    handler.db.list_playbook_activations.return_value = [_activation_row(ref)]

    result = await handler._cmd_set_playbook_enabled(
        {"playbook_id": ref.playbook_id, "enabled": False}
    )

    assert result == {
        "success": True,
        "playbook_id": ref.playbook_id,
        "enabled": False,
        "noop": False,
    }
    handler.db.set_playbook_activation.assert_awaited_once_with(
        playbook_id=ref.playbook_id,
        scope="system",
        scope_identifier="",
        artifact_sha256=ref.artifact_sha256,
        enabled=False,
        activated_by="operator",
        health="disabled",
        reasons="[]",
    )


async def test_set_playbook_enabled_resumes_with_ready_health():
    definition, ref, _activation = _backend_fixture()
    handler = _Handler(definition, ref, [])
    handler.db.list_playbook_activations.return_value = [_activation_row(ref, enabled=False)]

    result = await handler._cmd_set_playbook_enabled(
        {"playbook_id": ref.playbook_id, "enabled": True}
    )

    assert result["success"] is True and result["enabled"] is True
    kwargs = handler.db.set_playbook_activation.await_args.kwargs
    assert (kwargs["enabled"], kwargs["health"]) == (True, "ready")
    assert kwargs["artifact_sha256"] == ref.artifact_sha256


async def test_set_playbook_enabled_is_a_noop_when_already_in_that_state():
    definition, ref, _activation = _backend_fixture()
    handler = _Handler(definition, ref, [])
    handler.db.list_playbook_activations.return_value = [_activation_row(ref)]

    result = await handler._cmd_set_playbook_enabled(
        {"playbook_id": ref.playbook_id, "enabled": True}
    )

    assert result["noop"] is True
    handler.db.set_playbook_activation.assert_not_awaited()


async def test_set_playbook_enabled_refuses_to_enable_without_an_artifact():
    definition, ref, _activation = _backend_fixture()
    handler = _Handler(definition, ref, [])
    handler.db.list_playbook_activations.return_value = [
        _activation_row(ref, enabled=False, sha="")
    ]

    result = await handler._cmd_set_playbook_enabled(
        {"playbook_id": ref.playbook_id, "enabled": True}
    )

    assert "no active artifact" in result["error"]
    handler.db.set_playbook_activation.assert_not_awaited()


async def test_set_playbook_enabled_validates_its_arguments():
    definition, ref, _activation = _backend_fixture()
    handler = _Handler(definition, ref, [])
    handler.db.list_playbook_activations.return_value = []

    assert await handler._cmd_set_playbook_enabled({"enabled": True}) == {
        "error": "playbook_id is required"
    }
    assert await handler._cmd_set_playbook_enabled({"playbook_id": "p"}) == {
        "error": "enabled is required"
    }
    assert await handler._cmd_set_playbook_enabled(
        {"playbook_id": "p", "enabled": "yes"}
    ) == {"error": "enabled must be a boolean"}
    assert await handler._cmd_set_playbook_enabled({"playbook_id": "p", "enabled": True}) == {
        "error": "Playbook 'p' not found"
    }
