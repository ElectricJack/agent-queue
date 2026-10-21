"""Drift guard for the generated configuration-schema reference."""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "generate-config-schema-inventory.py"


def _generator():
    spec = importlib.util.spec_from_file_location("config_schema_inventory", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_render_is_the_editor_schema_and_contains_reload_metadata():
    generator = _generator()
    import json

    schema = json.loads(generator.render())
    assert schema["type"] == "object"
    assert schema["properties"]["scheduling"]["x-reload"] == "hot"
    assert "x-classification" in schema


def test_wait_policy_schema_exposes_nested_policies_canary_flags_and_hot_reload():
    import json

    policy = json.loads(_generator().render())["properties"]["wait_policy"]
    assert policy["x-reload"] == "hot"
    assert policy["additionalProperties"] is False
    props = policy["properties"]
    for name in ("v2_enabled", "hibernation_enabled", "scheduler_waits_enabled"):
        assert props[name]["default"] is False
    assert props["kinds"]["default"]["task"] == {
        "escalate_after_seconds": 14400, "expected_duration_seconds": 3600,
    }
    kind = props["kinds"]["additionalProperties"]
    assert kind["additionalProperties"] is False
    assert kind["properties"]["escalate_after_seconds"]["type"] == ["integer", "null"]
    project = props["projects"]["additionalProperties"]
    assert project["additionalProperties"] is False
    for name in ("v2_enabled", "hibernation_enabled", "scheduler_waits_enabled"):
        assert project["properties"][name]["type"] == ["boolean", "null"]
        assert project["properties"][name]["default"] is None


def test_check_detects_stale_output_then_accepts_regenerated_artifact(tmp_path, monkeypatch):
    generator = _generator()
    output = tmp_path / "configuration-schema.json"
    monkeypatch.setattr("sys.argv", ["generate-config-schema-inventory.py", "--output", str(output)])
    assert generator.main() == 0
    monkeypatch.setattr(
        "sys.argv", ["generate-config-schema-inventory.py", "--check", "--output", str(output)],
    )
    assert generator.main() == 0
    output.write_text("{}\n", encoding="utf-8")
    assert generator.main() == 1
