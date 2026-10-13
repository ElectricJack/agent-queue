"""Server-derived artifact requirements and executor compatibility.

Loading a definition is deliberately separate from supporting its execution: an
upgraded inspector can explain a newer artifact without ever running it. Empty
legacy pins are not rewritten; semantic inference also checks those at execution.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from src.playbooks.definition import (
    PlaybookDefinition,
    TrustedExecutionMetadata,
    step_values,
    uses_iteration_paths,
)
from src.playbooks.expressions import (
    ContextRef,
    DiffValue,
    FilterValue,
    LenValue,
    MapValue,
    condition_values,
)

KNOWN_FEATURES: Final = frozenset({
    "generated_commands_v1", "collections_v1", "nested_foreach_v1",
    "observation_context_v1", "state_v1", "script_v1",
})
# Add a feature only with its complete executor. Knowing its name is not support.
SUPPORTED_FEATURES: Final = KNOWN_FEATURES - {"script_v1"}
STATE_COMMANDS: Final = frozenset({
    "playbook_state_get", "playbook_state_list", "playbook_state_set",
    "playbook_state_delete", "playbook_state_notify", "playbook_state_inspect",
    "playbook_state_reset",
})


def referenced_commands(definition: PlaybookDefinition) -> tuple[str, ...]:
    """Commands used directly or exposed to an LLM, never capability guesses."""
    names: set[str] = set()
    for step in definition.steps.values():
        command = getattr(step, "command", None)
        if command:
            names.add(command)
        tools = getattr(step, "tool_use", None)
        if tools is not None:
            names.update(tools.aq_commands)
            names.update(tools.plugin_tools)
    return tuple(sorted(names))


def infer_required_features(
    definition: PlaybookDefinition, *, contracts: Any = None
) -> tuple[str, ...]:
    """Infer from typed semantics and the server's command descriptor projection.

    D1 and E3 extend the typed metadata/step models; their command hooks already
    reserve the feature requirement and fail closed until support is installed.
    """
    required: set[str] = set()
    values = [value for step in definition.steps.values() for value in step_values(step)]
    values.extend(value for rule in definition.rules for value in condition_values(rule.guard))
    if any(isinstance(value, (FilterValue, MapValue, LenValue, DiffValue)) for value in values):
        required.add("collections_v1")
    if any(isinstance(value, ContextRef) and value.path in {
        "observed_at", "observation_id"
    } for value in values):
        required.add("observation_context_v1")
    if uses_iteration_paths(definition):
        required.add("nested_foreach_v1")
    for name in referenced_commands(definition):
        if name in STATE_COMMANDS:
            required.add("state_v1")
        if name == "playbook_script_run":
            required.add("script_v1")
        contract = contracts.get(name) if contracts is not None else None
        if contract is not None:
            required.update(contract.required_features)
    metadata = definition.trusted_execution
    if metadata is not None and metadata.state_schema_version is not None:
        required.add("state_v1")
    if any(step.type == "script" for step in definition.steps.values()):
        required.add("script_v1")
    return tuple(sorted(required))


def execution_metadata_from_source(
    frontmatter: Mapping[str, Any], definition: PlaybookDefinition
) -> TrustedExecutionMetadata | None:
    """Pin the authored state version, defaulting to one only for a state user."""
    if "state_schema_version" in frontmatter or STATE_COMMANDS.intersection(
        referenced_commands(definition)
    ):
        if frontmatter.get("state_schema_version", 1) is None:
            raise ValueError("state_schema_version must be a positive integer")
        return TrustedExecutionMetadata(
            state_schema_version=frontmatter.get("state_schema_version", 1)
        )
    return None


def unsupported_features(
    definition: PlaybookDefinition,
    *,
    contracts: Any = None,
    supported_features: Iterable[str] | None = None,
) -> tuple[str, ...]:
    supported = SUPPORTED_FEATURES if supported_features is None else frozenset(supported_features)
    required = set(definition.required_features) | set(
        infer_required_features(definition, contracts=contracts)
    )
    return tuple(sorted(required - supported))


def unsupported_feature_message(feature: str) -> str:
    return (
        f"Artifact requires unsupported feature {feature!r}; upgrade all intended "
        "executors before activation, or explicitly restore a compatible reviewed artifact"
    )


class UnsupportedArtifactFeatures(ValueError):
    code = "unsupported_feature"

    def __init__(self, features: tuple[str, ...]) -> None:
        self.features = features
        super().__init__("; ".join(unsupported_feature_message(feature) for feature in features))


def require_supported_features(definition: PlaybookDefinition, *, contracts: Any = None) -> None:
    unsupported = unsupported_features(definition, contracts=contracts)
    if unsupported:
        raise UnsupportedArtifactFeatures(unsupported)
