"""Semantic graph command validation and response coverage."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.scope import AGENT_COMMAND_SET
from src.commands.handler import CommandHandler
from src.commands.playbook_v2_commands import PLAYBOOK_V2_COMMANDS
from src.config import LLMConfig

VALID_SHA = "sha256:" + "a" * 64

@dataclass
class _Playbooks:
    enabled: bool = True


def _make_handler() -> CommandHandler:
    mock_orch = MagicMock()
    mock_orch.db = AsyncMock()
    mock_orch.bus = AsyncMock()
    config = MagicMock()
    config.playbooks = _Playbooks()
    # A real section, not a mock: the AI cards resolve an ``llm`` node against
    # it, and a mock provider would make every direct-path model a mock too.
    config.llm = LLMConfig()
    return CommandHandler(mock_orch, config)


async def _call(handler: CommandHandler, name: str, args: dict) -> dict:
    return await getattr(handler, f"_cmd_{name}")(args)


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

class TestActivateArguments:
    async def test_artifact_sha256_is_required(self):
        handler = _make_handler()
        result = await _call(handler, "playbook_activate", {"playbook_id": "p"})
        assert result == {"error": "artifact_sha256 is required"}

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


def _backend_fixture():
    from pathlib import Path

    from src.playbooks.activation import ActivationHealth, ActivationHealthRecord
    from src.playbooks.artifact_ref import ArtifactRef
    from src.playbooks.definition import load_definition_json

    path = Path(__file__).parent / "fixtures/playbooks/v2/review-pipeline.artifact.json"
    definition = load_definition_json(path.read_text())
    ref = ArtifactRef(
        definition.id,
        definition.artifact_sha256(),
        2,
        definition.contract_fingerprint(),
        definition.source_hash,
        definition.compiler_build or "fixture",
        definition.compiled_at.isoformat(),
        definition.version,
    )
    activation = ActivationHealthRecord(
        "activation-1",
        definition.id,
        "system",
        "",
        True,
        ref.artifact_sha256,
        ActivationHealth.READY,
        (),
    )
    return definition, ref, activation


def _backend_handler():
    from types import SimpleNamespace

    from src.playbooks.validation import RegistryContractLookup
    from tests.playbook_v2_helpers import StubProfiles

    definition, ref, activation = _backend_fixture()
    handler = _make_handler()
    handler.db.get_playbook_artifact = AsyncMock(return_value=ref)
    handler.db.count_pending_events = AsyncMock(return_value=0)
    handler.db.count_active_runs = AsyncMock(return_value=0)
    handler._v2_health_records = AsyncMock(
        return_value=([activation], RegistryContractLookup(), StubProfiles())
    )
    store = MagicMock()
    store.load.return_value = definition
    handler._v2_engine = MagicMock(
        return_value=SimpleNamespace(services=SimpleNamespace(artifact_store=store))
    )
    return handler, definition, ref


async def test_v2_graph_returns_the_active_artifact():
    handler, _definition, ref = _backend_handler()
    result = await handler._cmd_playbook_v2_graph({"playbook_id": "default-pipeline"})
    assert result["artifact"]["artifact_sha256"] == ref.artifact_sha256
    assert len(result["nodes"]) == 13


async def test_v2_graph_honours_artifact_sha256():
    handler, _definition, ref = _backend_handler()
    result = await handler._cmd_playbook_v2_graph(
        {"playbook_id": "default-pipeline", "artifact_sha256": ref.artifact_sha256}
    )
    handler.db.get_playbook_artifact.assert_awaited_once_with(ref.artifact_sha256)
    assert result["artifact"]["artifact_sha256"] == ref.artifact_sha256


async def test_run_overlay_command_returns_pinned_artifact_ref():
    from src.playbooks.run_state import RunSnapshot

    handler, _definition, ref = _backend_handler()
    handler.db.load_run = AsyncMock(
        return_value=RunSnapshot(
            "run-1", "default-pipeline", ref.artifact_sha256, "review-on-task-completed"
        )
    )
    handler.db.count_receipts = AsyncMock(return_value=0)
    handler.db.list_receipts = AsyncMock(return_value=[])
    handler.db.list_playbook_activations = AsyncMock(return_value=[])
    result = await handler._cmd_playbook_run_overlay({"run_id": "run-1"})
    assert result["artifact"]["artifact_sha256"] == ref.artifact_sha256
    assert result["artifact_is_active"] is False


def _deep_high_class():
    from src.intelligence_classes import IntelligenceClass

    return IntelligenceClass(
        id="deep-high",
        name="Deep",
        description="",
        mapping={"anthropic": {"model": "claude-opus-5", "thinking": "high"}},
    )


def _reviewer_profile():
    from src.models import AgentProfile

    return AgentProfile(
        id="reviewer", name="Reviewer", harness="claude", default_class="deep-high"
    )


async def test_v2_lookups_carry_the_live_intelligence_classes():
    """The AI cards' provider/model policy is only resolvable with the snapshot."""
    from src.profiles.intelligence import ProfileIntelligence

    handler = _make_handler()
    handler.db.list_profiles = AsyncMock(return_value=[_reviewer_profile()])
    handler.orchestrator.intelligence_classes = {"deep-high": _deep_high_class()}
    _contracts, profiles, _events = await handler._v2_lookups()
    assert profiles.routing("reviewer") == ProfileIntelligence(
        "deep-high", "anthropic", "claude-opus-5"
    )


async def test_v2_lookups_carry_the_llm_config_for_direct_path_nodes():
    """swift-ember-68: an ``llm`` node resolves against ``llm:``, not the harness.

    Without the config the lookup would have to fall back to the harness's
    provider, which is the divergence between the card and the executor.
    """
    from src.config import LLMConfig
    from src.intelligence_classes import IntelligenceClass
    from src.profiles.intelligence import ProfileIntelligence

    handler = _make_handler()
    handler.config.llm = LLMConfig(provider="openai", api_key="k")
    handler.db.list_profiles = AsyncMock(return_value=[_reviewer_profile()])
    handler.orchestrator.intelligence_classes = {
        "deep-high": IntelligenceClass(
            id="deep-high",
            name="Deep",
            description="",
            mapping={
                "anthropic": {"model": "claude-opus-5"},
                "openai": {"model": "gpt-5"},
            },
        )
    }
    _contracts, profiles, _events = await handler._v2_lookups()
    # The profile's harness is claude; the direct path is configured openai.
    assert profiles.routing("reviewer") == ProfileIntelligence(
        "deep-high", "anthropic", "claude-opus-5"
    )
    assert profiles.direct_routing("reviewer") == ProfileIntelligence(
        "deep-high", "openai", "gpt-5"
    )


async def test_v2_graph_ai_nodes_report_the_resolved_provider_and_model():
    from src.playbooks.validation import RegistryContractLookup

    definition, ref, activation = _backend_fixture()
    handler = _backend_handler()[0]
    handler.db.list_profiles = AsyncMock(return_value=[_reviewer_profile()])
    handler.orchestrator.intelligence_classes = {"deep-high": _deep_high_class()}
    _contracts, profiles, _events = await handler._v2_lookups()
    handler._v2_health_records = AsyncMock(
        return_value=([activation], RegistryContractLookup(), profiles)
    )
    result = await handler._cmd_playbook_v2_graph({"playbook_id": definition.id})
    ai_nodes = [node["ai"] for node in result["nodes"] if node["ai"]]
    assert ai_nodes
    for ai in ai_nodes:
        assert (ai["intelligence_class"], ai["provider"], ai["model"]) == (
            "deep-high",
            "anthropic",
            "claude-opus-5",
        )
    assert ref.artifact_sha256 == result["artifact"]["artifact_sha256"]
