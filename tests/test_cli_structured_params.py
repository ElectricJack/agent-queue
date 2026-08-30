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

    def test_a_union_without_structure_is_a_plain_scalar_type(self):
        assert _schema_to_click_type({"type": ["string", "null"]}) is str
        assert _schema_to_click_type({"type": ["integer", "null"]}) is int


class TestUnaffectedSchemas:
    def test_scalars_are_unchanged(self):
        assert _schema_to_click_type({"type": "string"}) is str
        assert _schema_to_click_type({"type": "integer"}) is int
        assert _schema_to_click_type({"type": "number"}) is float
        assert _schema_to_click_type({"type": "boolean"}) is bool

    def test_enum_still_wins(self):
        param = _schema_to_click_type({"type": "string", "enum": ["a", "b"]})
        assert isinstance(param, click.Choice)

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
