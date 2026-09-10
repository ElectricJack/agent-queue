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
