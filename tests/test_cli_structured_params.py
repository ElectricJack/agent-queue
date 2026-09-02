"""Auto-generated CLI options carry structured values, not their text.

An ``object``- or ``array``-typed schema property used to fall through to
``str``, so the *text* was sent as the argument.  For ``update_config``
that is silent corruption: the YAML section becomes a quoted one-line
string, ``load_config``'s ``isinstance(raw[section], dict)`` guard stops
matching, and every field in it reverts to its dataclass default with
nothing logged.  ``aq system update-config --section swarm --data
'{"enabled": true}'`` therefore turned the swarm *off*.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import click
import pytest
from click.testing import CliRunner

from src.cli.auto_commands import (
    EXPLICIT_NULL,
    NullableParam,
    StructuredParam,
    _make_auto_command,
    _schema_to_click_type,
)


def convert(schema: dict, text: str):
    param_type = _schema_to_click_type(schema)
    assert isinstance(param_type, click.ParamType), f"expected a ParamType, got {param_type}"
    return param_type.convert(text, None, None)


class TestObjectAndArraySchemas:
    def test_object_property_parses_json(self):
        assert convert({"type": "object"}, '{"enabled": true, "n": 2}') == {
            "enabled": True,
            "n": 2,
        }

    def test_array_property_parses_json(self):
        assert convert({"type": "array"}, '["a", "b"]') == ["a", "b"]

    def test_array_property_accepts_a_bare_comma_list(self):
        """What people actually type."""
        assert convert({"type": "array"}, "a, b ,c") == ["a", "b", "c"]

    def test_object_property_rejects_a_json_array(self):
        with pytest.raises(click.UsageError):
            convert({"type": "object"}, '["a"]')

    def test_object_property_rejects_junk(self):
        with pytest.raises(click.UsageError):
            convert({"type": "object"}, "not json at all")


class TestUnionSchemas:
    #: ``update_config.data`` — the property this whole file exists for.
    UNION = {"type": ["object", "array", "string", "number", "boolean", "null"]}

    def test_a_union_containing_object_parses(self):
        assert convert(self.UNION, '{"enabled": true}') == {"enabled": True}

    def test_a_union_still_accepts_scalars(self):
        assert convert(self.UNION, "42") == 42
        assert convert(self.UNION, "true") is True

    def test_a_union_accepts_a_bare_string(self):
        """A section whose value really is a string must survive."""
        assert convert(self.UNION, "production") == "production"

    def test_null_becomes_the_explicit_sentinel(self):
        """Not a bare ``None`` — the callback would drop that as "not given"."""
        assert convert(self.UNION, "null") is EXPLICIT_NULL

    def test_a_union_without_structure_is_a_nullable_scalar_type(self):
        """It still converts scalars — but `null` is now sayable."""
        for schema, text, expected in (
            ({"type": ["string", "null"]}, "hello", "hello"),
            ({"type": ["integer", "null"]}, "7", 7),
            ({"type": ["number", "null"]}, "1.5", 1.5),
        ):
            param = _schema_to_click_type(schema)
            assert isinstance(param, NullableParam)
            assert param.convert(text, None, None) == expected
            assert param.convert("null", None, None) is EXPLICIT_NULL

    def test_a_nullable_scalar_still_rejects_junk(self):
        param = _schema_to_click_type({"type": ["integer", "null"]})
        with pytest.raises(click.UsageError):
            param.convert("nope", None, None)

    def test_null_is_case_and_whitespace_insensitive(self):
        param = _schema_to_click_type({"type": ["integer", "null"]})
        assert param.convert(" NULL ", None, None) is EXPLICIT_NULL

    def test_a_nullable_enum_keeps_its_choices_and_gains_null(self):
        """`edit_task.task_type` — enum members plus an explicit ``None``."""
        param = _schema_to_click_type(
            {"type": ["string", "null"], "enum": ["feature", "bugfix", None]}
        )
        assert isinstance(param, NullableParam)
        assert list(param.inner.choices) == ["feature", "bugfix", "null"]
        assert param.convert("BUGFIX", None, None) == "bugfix"
        assert param.convert("null", None, None) is EXPLICIT_NULL
        with pytest.raises(click.UsageError):
            param.convert("nonsense", None, None)


class TestUnaffectedSchemas:
    def test_scalars_are_unchanged(self):
        assert _schema_to_click_type({"type": "string"}) is str
        assert _schema_to_click_type({"type": "integer"}) is int
        assert _schema_to_click_type({"type": "number"}) is float
        assert _schema_to_click_type({"type": "boolean"}) is bool

    def test_enum_still_wins(self):
        param = _schema_to_click_type({"type": "string", "enum": ["a", "b"]})
        assert isinstance(param, click.Choice)

    def test_a_non_nullable_string_still_accepts_the_word_null(self):
        """Nothing that legitimately takes "null" as text may regress."""
        param = _schema_to_click_type({"type": "string"})
        assert param is str

    def test_already_structured_values_pass_through(self):
        assert StructuredParam("object").convert({"a": 1}, None, None) == {"a": 1}


UPDATE_CONFIG_TOOL = {
    "name": "update_config",
    "description": "Replace one top-level section in the YAML config.",
    "input_schema": {
        "type": "object",
        "properties": {
            "section": {"type": "string", "description": "Section to replace."},
            "data": {
                "type": ["object", "array", "string", "number", "boolean", "null"],
                "description": "New value. null to delete.",
            },
            "dry_run": {"type": "boolean", "description": "Validate only."},
        },
        "required": ["section", "data"],
    },
}


def _invoke(*argv: str) -> dict:
    """Run the generated `update-config` command, returning the args it sent."""
    from rich.console import Console

    command = _make_auto_command("update_config", "update-config", UPDATE_CONFIG_TOOL, Console())
    client = AsyncMock()
    client.execute = AsyncMock(return_value={"applied": True})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.cli.app._get_client", return_value=client):
        result = CliRunner().invoke(command, list(argv), obj={})
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once()
    return client.execute.await_args.args[1]


class TestArgsReachingTheServer:
    """End-to-end through the generated Click command, to the client call."""

    def test_explicit_null_is_sent_as_a_real_none(self):
        """This is what deletes a section — and it used to be dropped.

        The callback drops ``None`` kwargs (that is how "flag absent" is
        expressed), so a converted `null` never left the CLI and
        ``--data null`` silently did nothing.
        """
        args = _invoke("--section", "swarm", "--data", "null")
        assert args == {"section": "swarm", "data": None}
        assert "data" in args  # present, not merely falsy

    def test_an_omitted_option_is_still_dropped(self):
        args = _invoke("--section", "swarm", "--data", "{}")
        assert args == {"section": "swarm", "data": {}}
        assert "dry_run" not in args

    def test_an_object_arrives_parsed(self):
        args = _invoke("--section", "swarm", "--data", '{"enabled": true}')
        assert args["data"] == {"enabled": True}


POOL_SCALE_TOOL = {
    "name": "pool_scale",
    "description": "Set a pool's min/max bounds.",
    "input_schema": {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "Project ID."},
            "profile_id": {"type": "string", "description": "Pool profile ID."},
            "min": {"type": ["integer", "null"], "description": "New min_active."},
            "max": {
                "type": ["integer", "null"],
                "description": "New max_active; null removes the profile limit.",
            },
        },
        "required": ["project_id", "profile_id"],
    },
}


def _invoke_tool(tool: dict, cli_name: str, *argv: str) -> dict:
    """Run a generated command, returning the args it sent to the daemon."""
    from rich.console import Console

    command = _make_auto_command(tool["name"], cli_name, tool, Console())
    client = AsyncMock()
    client.execute = AsyncMock(return_value={"success": True})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("src.cli.app._get_client", return_value=client):
        result = CliRunner().invoke(command, list(argv), obj={})
    assert result.exit_code == 0, result.output
    client.execute.assert_awaited_once()
    return client.execute.await_args.args[1]


class TestNullableScalarOptionsReachTheServer:
    """`aq pool scale --max null` is the documented way to clear a cap."""

    def test_max_null_sends_the_key_with_a_none_value(self):
        args = _invoke_tool(
            POOL_SCALE_TOOL, "scale", "--project-id", "p", "--profile-id", "worker", "--max", "null"
        )
        assert args == {"project_id": "p", "profile_id": "worker", "max": None}
        assert "max" in args  # key presence is what `_cmd_pool_scale` branches on

    def test_an_omitted_max_leaves_the_key_absent(self):
        args = _invoke_tool(
            POOL_SCALE_TOOL, "scale", "--project-id", "p", "--profile-id", "worker", "--min", "1"
        )
        assert args == {"project_id": "p", "profile_id": "worker", "min": 1}
        assert "max" not in args

    def test_a_numeric_max_still_arrives_as_an_int(self):
        args = _invoke_tool(
            POOL_SCALE_TOOL, "scale", "--project-id", "p", "--profile-id", "worker", "--max", "4"
        )
        assert args["max"] == 4

    def test_a_non_integer_max_is_still_rejected(self):
        from rich.console import Console

        command = _make_auto_command("pool_scale", "scale", POOL_SCALE_TOOL, Console())
        result = CliRunner().invoke(
            command, ["--project-id", "p", "--profile-id", "w", "--max", "lots"], obj={}
        )
        assert result.exit_code != 0
        assert "not a valid integer" in result.output
