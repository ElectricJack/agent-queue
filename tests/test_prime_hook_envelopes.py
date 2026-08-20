"""Unit tests for ``src/prime/hook_envelopes.py`` and ``src/prime/overrides.py``.

Covers docs/specs/implementation/aq-surface.md §10.1: "hook envelope wrap +
suppression matrix" and the override template loader in isolation from the
full renderer (see tests/test_prime_renderer.py for end-to-end override
tests). Design refs: §5.3 (override), §5.4 (hook modes + suppression).
"""

from __future__ import annotations

import json

from src.prime.hook_envelopes import STARTUP_PROMPT_DELIVERED_ENV, suppressed, wrap
from src.prime.overrides import apply_override, load_override
from src.prime.models import PrimeDocument, PrimeSection


# ---------------------------------------------------------------------------
# wrap()
# ---------------------------------------------------------------------------


class TestWrap:
    def test_claude_harness_wraps_session_start_envelope(self):
        result = wrap("hello agent", "claude")
        payload = json.loads(result)
        assert payload == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "hello agent",
            }
        }

    def test_claude_harness_is_case_insensitive(self):
        result = wrap("body", "Claude")
        payload = json.loads(result)
        assert payload["hookSpecificOutput"]["additionalContext"] == "body"

    def test_unknown_harness_falls_back_to_plain_text(self):
        assert wrap("plain body", "codex") == "plain body"

    def test_empty_harness_falls_back_to_plain_text(self):
        assert wrap("plain body", "") == "plain body"

    def test_empty_body_wraps_to_empty_additional_context(self):
        payload = json.loads(wrap("", "claude"))
        assert payload["hookSpecificOutput"]["additionalContext"] == ""


# ---------------------------------------------------------------------------
# suppressed() — wrap/suppression matrix (design §5.4)
# ---------------------------------------------------------------------------


class TestSuppressed:
    def test_delivered_and_hook_mode_is_suppressed(self):
        env = {STARTUP_PROMPT_DELIVERED_ENV: "1"}
        assert suppressed(env, hook_mode=True) is True

    def test_delivered_but_not_hook_mode_is_not_suppressed(self):
        env = {STARTUP_PROMPT_DELIVERED_ENV: "1"}
        assert suppressed(env, hook_mode=False) is False

    def test_hook_mode_but_not_delivered_is_not_suppressed(self):
        env: dict[str, str] = {}
        assert suppressed(env, hook_mode=True) is False

    def test_neither_delivered_nor_hook_mode_is_not_suppressed(self):
        env: dict[str, str] = {}
        assert suppressed(env, hook_mode=False) is False

    def test_delivered_value_must_be_exactly_one(self):
        env = {STARTUP_PROMPT_DELIVERED_ENV: "true"}
        assert suppressed(env, hook_mode=True) is False

    def test_delivered_value_zero_is_not_suppressed(self):
        env = {STARTUP_PROMPT_DELIVERED_ENV: "0"}
        assert suppressed(env, hook_mode=True) is False


# ---------------------------------------------------------------------------
# load_override() / apply_override()
# ---------------------------------------------------------------------------


def _doc(**overrides) -> PrimeDocument:
    import datetime

    defaults = dict(
        task_id="t1",
        session_id=None,
        sections=(
            PrimeSection(key="role", title="Role", body="R body"),
            PrimeSection(key="task", title="Task", body="T body"),
        ),
        source="default",
        rendered_at=datetime.datetime.now(),
        work_dir="/wd",
        branch="main",
    )
    defaults.update(overrides)
    return PrimeDocument(**defaults)


class TestLoadOverride:
    def test_no_work_dir_returns_none(self):
        assert load_override(None) is None
        assert load_override("") is None

    def test_missing_file_returns_none(self, tmp_path):
        assert load_override(str(tmp_path)) is None

    def test_present_file_returns_contents(self, tmp_path):
        aq_dir = tmp_path / ".aq"
        aq_dir.mkdir()
        (aq_dir / "PRIME.md").write_text("hello override", encoding="utf-8")
        assert load_override(str(tmp_path)) == "hello override"


class TestApplyOverride:
    def test_substitutes_known_variables(self):
        doc = _doc()
        result = apply_override("[{{role}}][{{task}}]", doc)
        assert result == "[R body][T body]"

    def test_extra_variables_task_id_work_dir_branch(self):
        doc = _doc()
        result = apply_override("{{task.id}}/{{work_dir}}/{{branch}}", doc)
        assert result == "t1//wd/main"

    def test_unknown_variable_becomes_empty_string(self):
        doc = _doc()
        assert apply_override("[{{does_not_exist}}]", doc) == "[]"

    def test_template_without_variables_passes_through(self):
        doc = _doc()
        assert apply_override("static text, no vars", doc) == "static text, no vars"

    def test_no_double_substitution(self):
        # A section body that itself contains `{{...}}` must not be
        # re-substituted — apply_override runs exactly one pass.
        doc = _doc(
            sections=(PrimeSection(key="role", title="Role", body="contains {{task}} literally"),)
        )
        result = apply_override("{{role}}", doc)
        assert result == "contains {{task}} literally"
