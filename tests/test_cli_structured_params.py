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

import click
import pytest

from src.cli.auto_commands import StructuredParam, _schema_to_click_type


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

    def test_null_deletes_the_section(self):
        assert convert(self.UNION, "null") is None

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
