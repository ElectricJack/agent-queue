from src.profiles.parser import parse_profile, parsed_profile_to_agent_profile


MD = """---
id: worker
name: Worker
---

## Config
```json
{"default_class": "standard", "needs_workspace": false}
```
"""


def test_parser_captures_default_class_and_needs_workspace():
    parsed = parse_profile(MD)
    assert parsed.is_valid, parsed.errors
    assert parsed.config["default_class"] == "standard"
    assert parsed.config["needs_workspace"] is False


def test_agent_profile_dict_carries_the_new_fields():
    parsed = parse_profile(MD)
    d = parsed_profile_to_agent_profile(parsed)
    assert d["default_class"] == "standard"
    assert d["needs_workspace"] is False


def test_needs_workspace_must_be_bool():
    parsed = parse_profile(MD.replace("false", '"nope"'))
    assert not parsed.is_valid
    assert any("needs_workspace" in e for e in parsed.errors)
