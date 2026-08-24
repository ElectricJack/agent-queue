"""Tests for prompt-builder tier budget warnings and hub-file skipping.

Two log-hygiene regressions are covered here:

1. The L0 budget was 50 tokens, describing only the ``## Role`` sentence,
   while ``set_l0_role_from_markdown`` composes Role + Rules + Reflection.
   Every well-formed shipped profile therefore tripped the overflow warning
   on every prompt build.
2. Overflow warnings fired once per prompt build — dozens of identical
   lines per playbook run — burying every other warning in the log.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.prompt_builder import (
    _CHARS_PER_TOKEN,
    _L0_TOKEN_BUDGET,
    PromptBuilder,
    reset_tier_overflow_warnings,
)
from src.vault_index import is_generated_hub_file


@pytest.fixture(autouse=True)
def _clear_warning_cache():
    reset_tier_overflow_warnings()
    yield
    reset_tier_overflow_warnings()


def _role_block(approx_tokens: int) -> str:
    """Build a Role+Rules+Reflection block of roughly *approx_tokens* tokens."""
    return "## Role\n" + ("word " * (approx_tokens * _CHARS_PER_TOKEN // 5))


class TestL0Budget:
    def test_budget_accommodates_role_rules_reflection(self):
        """A realistic composed L0 block must not warn.

        The shipped supervisor profile's Role+Rules+Reflection block is
        ~1700 chars (~425 tokens); it is legitimate content, not drift.
        """
        assert _L0_TOKEN_BUDGET >= 400

    def test_realistic_supervisor_sized_role_does_not_warn(self, caplog):
        builder = PromptBuilder()
        text = _role_block(425)
        with caplog.at_level(logging.WARNING, logger="src.prompt_builder"):
            builder.set_l0_role(text)
        assert "L0 role" not in caplog.text

    def test_genuinely_runaway_role_still_warns(self, caplog):
        builder = PromptBuilder()
        text = _role_block(_L0_TOKEN_BUDGET * 3)
        with caplog.at_level(logging.WARNING, logger="src.prompt_builder"):
            builder.set_l0_role(text)
        assert "L0 role text is" in caplog.text
        assert "consider trimming" in caplog.text


class TestOverflowWarningDedupe:
    def test_same_text_warns_only_once(self, caplog):
        text = _role_block(_L0_TOKEN_BUDGET * 3)
        with caplog.at_level(logging.WARNING, logger="src.prompt_builder"):
            for _ in range(40):
                PromptBuilder().set_l0_role(text)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f"expected 1 warning, got {len(warnings)}"

    def test_repeats_are_demoted_to_debug(self, caplog):
        text = _role_block(_L0_TOKEN_BUDGET * 3)
        with caplog.at_level(logging.DEBUG, logger="src.prompt_builder"):
            PromptBuilder().set_l0_role(text)
            caplog.clear()
            PromptBuilder().set_l0_role(text)

        assert [r.levelno for r in caplog.records] == [logging.DEBUG]
        assert "already warned" in caplog.text

    def test_distinct_overflowing_texts_each_warn(self, caplog):
        a = _role_block(_L0_TOKEN_BUDGET * 3)
        b = a + "\n\n## Rules\ndifferent content entirely"
        with caplog.at_level(logging.WARNING, logger="src.prompt_builder"):
            PromptBuilder().set_l0_role(a)
            PromptBuilder().set_l0_role(b)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2

    def test_reset_re_arms_the_warning(self, caplog):
        text = _role_block(_L0_TOKEN_BUDGET * 3)
        with caplog.at_level(logging.WARNING, logger="src.prompt_builder"):
            PromptBuilder().set_l0_role(text)
            reset_tier_overflow_warnings()
            PromptBuilder().set_l0_role(text)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2


class TestGeneratedHubFileDetection:
    @pytest.mark.parametrize(
        "path",
        [
            "vault/intelligence-classes/intelligence-classes.md",
            "vault/workspace-kinds/workspace-kinds.md",
            "vault/mcp-servers/mcp-servers.md",
            "vault/agent-types/agent-types.md",
        ],
    )
    def test_hub_files_are_detected(self, path):
        assert is_generated_hub_file(Path(path))

    @pytest.mark.parametrize(
        "path",
        [
            "vault/workspace-kinds/project-repo.md",
            "vault/workspace-kinds/vault.md",
            "vault/intelligence-classes/fast-low.md",
            "vault/mcp-servers/google-docs.md",
        ],
    )
    def test_real_config_files_are_not_hub_files(self, path):
        assert not is_generated_hub_file(Path(path))

    def test_accepts_str_paths(self):
        assert is_generated_hub_file("vault/workspace-kinds/workspace-kinds.md")
