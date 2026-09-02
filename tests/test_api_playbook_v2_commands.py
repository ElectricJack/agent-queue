"""Playbook V2 semantic-graph commands — ``src/commands/playbook_v2_commands.py``.

Covers the three gates every one of the seven commands passes through, in
order: the ``playbooks.v2_api`` read flag, argument validation, and (for the
two operator writes) the separate ``playbooks.v2_activation_writes`` flag.

The typed artifact model, artifact store and run receipts (Packages 2-4 of the
Playbook V2 roadmap) are not present on ``main``, so every command reports
``V2_STORAGE_UNAVAILABLE_ERROR`` at the single seam where it would read that
state.  These tests pin the surface around that seam: the flags, the exact
error strings, the validation, and the fact that neither write command is
reachable from an agent session token.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.scope import AGENT_COMMAND_SET
from src.commands.handler import CommandHandler
from src.commands.playbook_v2_commands import (
    PLAYBOOK_V2_COMMANDS,
    V2_API_DISABLED_ERROR,
    V2_STORAGE_UNAVAILABLE_ERROR,
    V2_WRITES_DISABLED_ERROR,
)

VALID_SHA = "sha256:" + "a" * 64
OTHER_SHA = "sha256:" + "b" * 64

READ_COMMANDS = {
    "playbook_v2_graph": {"playbook_id": "default-pipeline"},
    "playbook_activation_health": {},
    "playbook_artifact_diff": {"playbook_id": "p", "target_sha256": VALID_SHA},
    "playbook_pending_events": {},
    "playbook_run_overlay": {"run_id": "run-1"},
}

WRITE_COMMANDS = {
    "playbook_activate": {"playbook_id": "p", "artifact_sha256": VALID_SHA},
    "playbook_pending_event_action": {"action": "discard", "pending_event_ids": ["e1"]},
}

ALL_COMMANDS = {**READ_COMMANDS, **WRITE_COMMANDS}


@dataclass
class _Playbooks:
    enabled: bool = True
    v2_api: bool = False
    v2_activation_writes: bool = False


def _make_handler(*, v2_api: bool = True, v2_activation_writes: bool = True) -> CommandHandler:
    mock_orch = MagicMock()
    mock_orch.db = AsyncMock()
    mock_orch.bus = AsyncMock()
    config = MagicMock()
    config.playbooks = _Playbooks(v2_api=v2_api, v2_activation_writes=v2_activation_writes)
    return CommandHandler(mock_orch, config)


async def _call(handler: CommandHandler, name: str, args: dict) -> dict:
    return await getattr(handler, f"_cmd_{name}")(args)


class TestFeatureFlags:
    @pytest.mark.parametrize("name", sorted(ALL_COMMANDS))
    async def test_every_command_is_refused_when_the_read_flag_is_off(self, name):
        handler = _make_handler(v2_api=False, v2_activation_writes=True)
        result = await _call(handler, name, ALL_COMMANDS[name])
        assert result == {"error": V2_API_DISABLED_ERROR}

    @pytest.mark.parametrize("name", sorted(WRITE_COMMANDS))
    async def test_writes_are_refused_independently_of_reads(self, name):
        """Roadmap Package 5 rollback boundary: the whole review surface stays
        readable with activation writes disabled."""
        handler = _make_handler(v2_api=True, v2_activation_writes=False)
        result = await _call(handler, name, WRITE_COMMANDS[name])
        assert result == {"error": V2_WRITES_DISABLED_ERROR}

    @pytest.mark.parametrize("name", sorted(READ_COMMANDS))
    async def test_reads_still_work_when_only_writes_are_disabled(self, name):
        handler = _make_handler(v2_api=True, v2_activation_writes=False)
        result = await _call(handler, name, READ_COMMANDS[name])
        assert result == {"error": V2_STORAGE_UNAVAILABLE_ERROR}

    @pytest.mark.parametrize("name", sorted(ALL_COMMANDS))
    async def test_flags_default_to_off(self, name):
        """A daemon that has never heard of the V2 surface refuses it."""
        from src.config import PlaybooksConfig

        defaults = PlaybooksConfig()
        assert defaults.v2_api is False
        assert defaults.v2_activation_writes is False


class TestStorageSeam:
    @pytest.mark.parametrize("name", sorted(ALL_COMMANDS))
    async def test_valid_arguments_reach_the_storage_seam(self, name):
        """With both flags on and valid arguments, every command reports the
        one honest error: the Package 2-4 state it projects does not exist."""
        handler = _make_handler()
        result = await _call(handler, name, ALL_COMMANDS[name])
        assert result == {"error": V2_STORAGE_UNAVAILABLE_ERROR}


class TestGraphArguments:
    async def test_playbook_id_is_required(self):
        handler = _make_handler()
        assert await _call(handler, "playbook_v2_graph", {}) == {"error": "playbook_id is required"}
        assert await _call(handler, "playbook_v2_graph", {"playbook_id": "  "}) == {
            "error": "playbook_id is required"
        }

    async def test_direction_is_validated(self):
        handler = _make_handler()
        result = await _call(
            handler, "playbook_v2_graph", {"playbook_id": "p", "direction": "sideways"}
        )
        assert result == {"error": "Invalid direction 'SIDEWAYS'. Valid: TD, LR"}

    @pytest.mark.parametrize("direction", ["TD", "LR", "td", "lr"])
    async def test_direction_is_case_insensitive(self, direction):
        handler = _make_handler()
        result = await _call(
            handler, "playbook_v2_graph", {"playbook_id": "p", "direction": direction}
        )
        assert result == {"error": V2_STORAGE_UNAVAILABLE_ERROR}

    @pytest.mark.parametrize(
        "sha",
        ["abc", "sha256:abc", "a" * 64, "sha256:" + "A" * 64, "sha256:" + "a" * 63],
    )
    async def test_truncated_or_malformed_hashes_are_rejected(self, sha):
        """Hashes are never truncated on the wire, so a short one is a client
        bug worth an explicit error rather than a silent miss."""
        handler = _make_handler()
        result = await _call(
            handler, "playbook_v2_graph", {"playbook_id": "p", "artifact_sha256": sha}
        )
        assert result == {"error": "artifact_sha256 must be a full 'sha256:<64 hex>' digest"}


class TestActivationHealthArguments:
    async def test_scope_is_validated(self):
        handler = _make_handler()
        result = await _call(handler, "playbook_activation_health", {"scope": "team"})
        assert "Invalid scope 'team'" in result["error"]

    async def test_health_filter_is_validated(self):
        handler = _make_handler()
        result = await _call(handler, "playbook_activation_health", {"health": "needs_rebuild"})
        assert "Invalid health 'needs_rebuild'" in result["error"]

    @pytest.mark.parametrize(
        "health",
        ["ready", "question_required", "invalid", "disabled", "stale_contract", "unavailable"],
    )
    async def test_all_six_health_values_are_accepted(self, health):
        handler = _make_handler()
        result = await _call(handler, "playbook_activation_health", {"health": health})
        assert result == {"error": V2_STORAGE_UNAVAILABLE_ERROR}


class TestArtifactDiffArguments:
    async def test_target_sha256_is_required(self):
        handler = _make_handler()
        result = await _call(handler, "playbook_artifact_diff", {"playbook_id": "p"})
        assert result == {"error": "target_sha256 is required"}

    async def test_base_sha256_is_validated_when_supplied(self):
        handler = _make_handler()
        result = await _call(
            handler,
            "playbook_artifact_diff",
            {"playbook_id": "p", "target_sha256": VALID_SHA, "base_sha256": "sha256:zz"},
        )
        assert result == {"error": "base_sha256 must be a full 'sha256:<64 hex>' digest"}

    async def test_base_sha256_may_be_omitted(self):
        """Omitted means "the currently active artifact", or nothing at all for
        a playbook's first artifact."""
        handler = _make_handler()
        result = await _call(
            handler, "playbook_artifact_diff", {"playbook_id": "p", "target_sha256": VALID_SHA}
        )
        assert result == {"error": V2_STORAGE_UNAVAILABLE_ERROR}


class TestActivateArguments:
    async def test_artifact_sha256_is_required(self):
        handler = _make_handler()
        result = await _call(handler, "playbook_activate", {"playbook_id": "p"})
        assert result == {"error": "artifact_sha256 is required"}

    async def test_acknowledgement_cannot_be_replayed_against_another_artifact(self):
        """§7.3: ``acknowledge_diff`` is the literal target hash."""
        handler = _make_handler()
        result = await _call(
            handler,
            "playbook_activate",
            {"playbook_id": "p", "artifact_sha256": VALID_SHA, "acknowledge_diff": OTHER_SHA},
        )
        assert "acknowledge_diff must equal artifact_sha256" in result["error"]

    async def test_matching_acknowledgement_passes_validation(self):
        handler = _make_handler()
        result = await _call(
            handler,
            "playbook_activate",
            {"playbook_id": "p", "artifact_sha256": VALID_SHA, "acknowledge_diff": VALID_SHA},
        )
        assert result == {"error": V2_STORAGE_UNAVAILABLE_ERROR}


class TestPendingEventArguments:
    async def test_reason_filter_is_validated(self):
        handler = _make_handler()
        result = await _call(handler, "playbook_pending_events", {"reason": "bored"})
        assert "Invalid reason 'bored'" in result["error"]

    @pytest.mark.parametrize("limit", ["not-a-number", None])
    async def test_limit_must_be_an_integer(self, limit):
        handler = _make_handler()
        result = await _call(handler, "playbook_pending_events", {"limit": limit})
        assert result == {"error": "limit must be an integer"}

    async def test_limit_must_be_positive(self):
        handler = _make_handler()
        result = await _call(handler, "playbook_pending_events", {"limit": 0})
        assert result == {"error": "limit must be >= 1"}

    async def test_action_is_validated(self):
        handler = _make_handler()
        result = await _call(
            handler,
            "playbook_pending_event_action",
            {"action": "replay", "pending_event_ids": ["e1"]},
        )
        assert "Invalid action 'replay'" in result["error"]

    async def test_action_is_required(self):
        handler = _make_handler()
        result = await _call(
            handler, "playbook_pending_event_action", {"pending_event_ids": ["e1"]}
        )
        assert result == {"error": "action is required"}

    @pytest.mark.parametrize("ids", [None, [], ["", "   "], 7])
    async def test_pending_event_ids_must_be_a_non_empty_list(self, ids):
        handler = _make_handler()
        result = await _call(
            handler, "playbook_pending_event_action", {"action": "discard", "pending_event_ids": ids}
        )
        assert "pending_event_ids" in result["error"]

    async def test_a_bare_string_id_is_accepted_as_a_single_id(self):
        handler = _make_handler()
        result = await _call(
            handler,
            "playbook_pending_event_action",
            {"action": "dispatch", "pending_event_ids": "e1"},
        )
        assert result == {"error": V2_STORAGE_UNAVAILABLE_ERROR}


class TestRunOverlayArguments:
    async def test_run_id_is_required(self):
        handler = _make_handler()
        assert await _call(handler, "playbook_run_overlay", {}) == {"error": "run_id is required"}

    async def test_receipt_limit_is_validated(self):
        handler = _make_handler()
        assert await _call(handler, "playbook_run_overlay", {"run_id": "r", "receipt_limit": 0}) == {
            "error": "receipt_limit must be >= 1"
        }
        assert await _call(
            handler, "playbook_run_overlay", {"run_id": "r", "receipt_limit": "many"}
        ) == {"error": "receipt_limit must be an integer"}


class TestScope:
    def test_v2_commands_are_out_of_scope_for_agent_sessions(self):
        """§7.2: this package adds nothing to any server-owned allowlist, so a
        session token cannot reach the graph, the diff, or either write."""
        assert PLAYBOOK_V2_COMMANDS & AGENT_COMMAND_SET == frozenset()
