"""The retired ``runtime`` config key is rejected with a pointer to ``harness``.

The in-process Supervisor was the last Runtime implementation; with it gone
every agent runs as a tmux session and ``harness`` is the only selector.  A
profile still carrying ``runtime`` was written for a dispatch path that no
longer exists, so the key is rejected rather than silently ignored.
"""

from __future__ import annotations

from src.profiles.parser import _validate_config


def test_runtime_key_rejected_with_pointer_to_harness():
    errors = _validate_config({"runtime": "supervisor", "harness": "claude"})
    assert any("'runtime' was removed" in e and "harness" in e for e in errors)


def test_harness_only_is_fine():
    assert not [e for e in _validate_config({"harness": "claude"}) if "runtime" in e]
