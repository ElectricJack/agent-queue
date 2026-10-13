"""JSON-object contracts for uncovered commands; live invocation remains gated.

A2 owns the return-protocol and authorization audit; A3 owns private values and
safe inspection. This foundation deliberately has no switch that can bypass those
release gates. Catalogue membership alone can never call a newly discovered verb.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar

from jsonschema import validators
from jsonschema.exceptions import SchemaError
from pydantic import ConfigDict, RootModel, model_validator
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

from src.commands.contracts.models import (
    CommandContract,
    CommandPresentation,
    ExecutionContract,
    IdempotencySpec,
    OutcomeClass,
    OutcomeSpec,
    SideEffectClass,
)
from src.commands.descriptors import CommandDescriptor


def validate_json(value: Any) -> None:
    """Reject Python-only values and coercion before Pydantic sees the object."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            validate_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            validate_json(item)
        return
    raise ValueError("Expected finite JSON values with string object keys")


def _no_remote_resource(uri: str):
    raise NoSuchResource(ref=uri)


def _local_references_only(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                not isinstance(item, str) or not item.startswith("#")
            ):
                raise ValueError("Only local JSON Schema references are allowed")
            if key in {"$defs", "definitions", "properties", "patternProperties",
                       "dependentSchemas", "dependencies"} and isinstance(item, dict):
                for child in item.values():
                    _local_references_only(child)
            elif key in {"items", "prefixItems", "additionalItems", "additionalProperties",
                         "unevaluatedProperties", "unevaluatedItems", "contains", "propertyNames",
                         "if", "then", "else", "not", "allOf", "anyOf", "oneOf"}:
                _local_references_only(item)
    elif isinstance(value, list):
        for item in value:
            _local_references_only(item)


def schema_validator(schema: dict):
    _local_references_only(schema)
    validator = validators.validator_for(schema)
    validator.check_schema(schema)
    return validator(schema, registry=Registry(retrieve=_no_remote_resource))


class JsonObject(RootModel[dict[str, Any]]):
    """A declared model whose dump is the original object, with no wrapper key."""

    model_config = ConfigDict(frozen=True)

    @model_validator(mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> Any:
        if type(value) is not dict:
            raise ValueError("Expected a JSON object")
        validate_json(value)
        return value


class JsonObjectArgs(JsonObject):
    _schema: ClassVar[dict[str, Any]] = {"type": "object", "additionalProperties": True}
    _validator: ClassVar[Any] = None
    _schema_error: ClassVar[str | None] = None

    @model_validator(mode="before")
    @classmethod
    def _validate_schema(cls, value: Any) -> Any:
        validate_json(value)
        if cls._validator is None and cls._schema_error is None:
            # Catalogue/transport discovery needs the document, not a compiled
            # validator for every command. Compile only on first actual input.
            try:
                cls._validator = schema_validator(cls._schema)
            except (ValueError, SchemaError):
                cls._schema_error = "Invalid JSON Schema or non-local schema reference"
        if cls._schema_error:
            raise ValueError(cls._schema_error)
        try:
            error = next(cls._validator.iter_errors(value), None)
        except Unresolvable as exc:
            raise ValueError("JSON Schema reference cannot be resolved locally") from exc
        if error is not None:
            # Do not include arbitrary argument values in public validation errors.
            path = "/".join(map(str, error.absolute_path)) or "<root>"
            raise ValueError(f"JSON Schema validation failed at {path}: {error.validator}")
        return value

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        import copy

        return copy.deepcopy(cls._schema)


class JsonObjectResult(JsonObject):
    pass


def argument_model(descriptor: CommandDescriptor) -> type[JsonObjectArgs]:
    schema = descriptor.input_schema("playbook")
    # Preserve even invalid plugin schemas for inspection. Validation rejects
    # their inputs without hiding the catalogue or doing remote retrieval.
    return type(
        f"{descriptor.name}Arguments", (JsonObjectArgs,),
        {"_schema": schema, "_validator": None, "_schema_error": None, "__module__": __name__},
    )


class GeneratedInvocationDisabled(RuntimeError):
    """The generated command audit and confidentiality gates are not complete."""


def generated_registration(descriptor: CommandDescriptor):
    from src.commands.contracts.registry import CommandRegistration

    async def invoke(args: JsonObjectArgs, principal: Any):
        # No handler lookup, dispatch, principal substitution or preview fallback
        # is permitted before A2/A3 supply and verify the execution boundary.
        reason = descriptor.denial_reason or (
            "command_unavailable" if not descriptor.available else
            "generated_invocation_disabled: authorization and private-value gates required"
        )
        raise GeneratedInvocationDisabled(reason)

    return CommandRegistration(
        descriptor.name,
        CommandContract(
            execution=ExecutionContract(
                name=descriptor.name, args_model=argument_model(descriptor),
                result_model=JsonObjectResult, argument_mode="json_object",
                result_mode="json_object", schema_generation=2,
                descriptor_metadata=descriptor.execution_metadata(),
                outcomes=(
                    OutcomeSpec(name="success", classification=OutcomeClass.SUCCESS),
                    OutcomeSpec(name="failure", classification=OutcomeClass.FAILURE),
                ),
                capability=descriptor.capability, side_effect=SideEffectClass.COMPOSITE,
                idempotency=IdempotencySpec(mode="none"), retry_safe=False,
            ),
            presentation=CommandPresentation(
                title=descriptor.name.replace("_", " ").title(), summary=descriptor.description,
            ),
        ),
        invoke,
    )
