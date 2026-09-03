"""Playbook V2 API DTO contract — ``src/api/models/playbook_v2.py``.

The DTO module is the frozen interface contract of the Package 5 child plan
(``docs/superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md`` §4): it is
what lets the backend and dashboard slices proceed in parallel.  These tests
pin the properties the parallel slices depend on — strictness, registration,
serialization of explicit nulls, and reachability of the seven commands through
the codegen surface.
"""

from __future__ import annotations

import inspect

from pydantic import BaseModel

from src.api import codegen
from src.api import models as api_models
from src.api.models import playbook_v2
from src.commands.handler import PAUSED_PLAYBOOK_COMMANDS
from src.commands.playbook_v2_commands import (
    PLAYBOOK_V2_COMMANDS,
    PLAYBOOK_V2_COMPILER_COMMANDS,
)
from src.tools.definitions import _ALL_TOOL_DEFINITIONS, _TOOL_CATEGORIES

SEVEN_COMMANDS = {
    "playbook_v2_graph",
    "playbook_activation_health",
    "playbook_activate",
    "playbook_artifact_diff",
    "playbook_pending_events",
    "playbook_pending_event_action",
    "playbook_run_overlay",
}

COMPILER_COMMANDS = {
    "playbook_v2_validate",
    "playbook_v2_propose",
    "playbook_v2_shadow_compile",
}
ALL_V2_COMMANDS = SEVEN_COMMANDS | COMPILER_COMMANDS


def _v2_models() -> list[type[BaseModel]]:
    return [
        obj
        for _, obj in inspect.getmembers(playbook_v2, inspect.isclass)
        if issubclass(obj, BaseModel) and obj.__module__ == playbook_v2.__name__
    ]


class TestStrictness:
    def test_every_v2_model_forbids_extra_keys(self):
        """An unknown key is a contract break, not a warning (roadmap §2)."""
        models = _v2_models()
        assert models, "no models discovered in src.api.models.playbook_v2"
        offenders = [m.__name__ for m in models if m.model_config.get("extra") != "forbid"]
        assert offenders == []

    def test_extra_key_is_rejected_at_construction(self):
        from pydantic import ValidationError

        try:
            playbook_v2.GridPositionDTO(x=1, y=2, z=3)
        except ValidationError:
            pass
        else:  # pragma: no cover - the assertion above is the contract
            raise AssertionError("extra='forbid' did not reject an unknown key")


class TestRegistration:
    def test_response_models_registered_for_seven_commands(self):
        merged = api_models.get_all_response_models()
        assert SEVEN_COMMANDS <= set(merged)
        for name, model in playbook_v2.RESPONSE_MODELS.items():
            assert merged[name] is model

    def test_response_models_dict_covers_package_two_and_five_surfaces(self):
        assert set(playbook_v2.RESPONSE_MODELS) == ALL_V2_COMMANDS
        assert PLAYBOOK_V2_COMMANDS == frozenset(SEVEN_COMMANDS)
        assert PLAYBOOK_V2_COMPILER_COMMANDS == frozenset(COMPILER_COMMANDS)

    def test_v2_commands_are_not_in_response_exclude_none(self):
        """Optional blocks serialize as explicit ``null`` so the TS client can
        type optionality.  ``RESPONSE_EXCLUDE_NONE`` is the V1 graph hack; this
        is the ratchet that keeps the V2 commands out of it."""
        assert ALL_V2_COMMANDS & codegen.RESPONSE_EXCLUDE_NONE == set()

    def test_every_command_has_a_tool_definition_and_category(self):
        """Without both, a command gets no HTTP route and no CLI verb."""
        defined = {t["name"] for t in _ALL_TOOL_DEFINITIONS}
        assert ALL_V2_COMMANDS <= defined
        for name in ALL_V2_COMMANDS:
            assert _TOOL_CATEGORIES[name] == "playbook"

    def test_every_command_pauses_with_the_playbook_subsystem(self):
        assert SEVEN_COMMANDS <= PAUSED_PLAYBOOK_COMMANDS

    def test_every_command_is_implemented_on_the_handler(self):
        from src.commands.handler import CommandHandler

        for name in SEVEN_COMMANDS:
            assert hasattr(CommandHandler, f"_cmd_{name}"), name


class TestSerializationConventions:
    def test_optional_blocks_serialize_as_explicit_null(self):
        dumped = playbook_v2.ArtifactRefDTO(
            playbook_id="p",
            artifact_sha256="sha256:" + "a" * 64,
            schema_generation=2,
            contract_fingerprint="sha256:" + "b" * 64,
            source_digest="sha256:" + "c" * 64,
            compiler_build="aq-compiler/test",
        ).model_dump()
        assert "compiled_at" in dumped
        assert dumped["compiled_at"] is None

    def test_every_response_model_emits_a_json_schema(self):
        """Codegen builds the OpenAPI snapshot from these; a model that cannot
        produce a schema breaks both generated clients."""
        for name, model in playbook_v2.RESPONSE_MODELS.items():
            schema = model.model_json_schema()
            assert schema["type"] == "object", name

    def test_graph_response_resolves_activation_without_model_rebuild(self):
        """§4.8: the activation block is defined above the graph block so no
        forward reference needs resolving."""
        assert playbook_v2.PlaybookV2GraphResponse.__pydantic_complete__
        field = playbook_v2.PlaybookV2GraphResponse.model_fields["activation"]
        assert field.annotation is playbook_v2.ActivationStateDTO


class TestDelegationNarrowing:
    """The AI-node card's third intersection term (roadmap §2)."""

    def test_delegation_projects_the_step_narrowing(self):
        dto = playbook_v2.DelegationPolicyDTO(
            child_profile_id="reviewer",
            capability_narrowing=playbook_v2.CapabilityNarrowingDTO(
                harness_tools=["Grep", "Read"], aq_commands=[]
            ),
        )
        dumped = dto.model_dump()
        assert dumped["capability_narrowing"]["harness_tools"] == ["Grep", "Read"]
        # ``[]`` means none and ``None`` means "narrows nothing here"; the card
        # has to be able to tell those apart, so both survive serialization.
        assert dumped["capability_narrowing"]["aq_commands"] == []
        assert dumped["capability_narrowing"]["plugin_tools"] is None

    def test_a_step_without_narrowing_serializes_an_explicit_null(self):
        dumped = playbook_v2.DelegationPolicyDTO(child_profile_id="reviewer").model_dump()
        assert "capability_narrowing" in dumped
        assert dumped["capability_narrowing"] is None

    def test_the_narrowing_dto_covers_every_capability_namespace(self):
        from src.profiles.capabilities import NAMESPACES

        assert set(playbook_v2.CapabilityNarrowingDTO.model_fields) == set(NAMESPACES)


class TestHealthAndEnums:
    def test_activation_health_carries_all_six_values(self):
        """Roadmap §4's five, plus the transient ``unavailable`` §2.1 adds."""
        from typing import get_args

        assert set(get_args(playbook_v2.ActivationHealthValue)) == {
            "ready",
            "question_required",
            "invalid",
            "disabled",
            "stale_contract",
            "unavailable",
        }

    def test_activation_defaults_to_disabled(self):
        state = playbook_v2.ActivationStateDTO(playbook_id="p", scope="system")
        assert state.enabled is False
        assert state.health == "disabled"
        assert state.active_artifact_sha256 is None
