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
        }
    )
    assert result["blocked"] is False
    assert handler.db.set_playbook_activation.await_args.kwargs["activated_by"] == "local"


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
