"""Tests for the PlaybookCompiler — LLM-powered markdown-to-JSON compilation.

Tests cover:
- Frontmatter parsing and validation
- Source hash computation
- JSON extraction from LLM responses (fenced blocks, bare JSON)
- Frontmatter merging into compiled output
- Full compilation pipeline (with mocked LLM)
- Retry logic on validation failures
- Error handling (LLM failures, malformed output)
- Compilation happy-path per roadmap 5.1.8 (a)-(g)
"""

from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import AsyncMock

import pytest

from src.chat_providers.types import ChatResponse, TextBlock
from src.playbook_compiler import CompilationResult, PlaybookCompiler
from src.playbook_models import CompiledPlaybook, generate_json_schema
from src.playbook_store import CompiledPlaybookStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SIMPLE_PLAYBOOK_MD = """\
---
id: code-quality-gate
triggers:
  - git.commit
scope: system
---

# Code Quality Gate

When a commit is made, run vibecop on the changed files. If issues
are found, create tasks to fix them. If no issues, we're done.
"""

VALID_COMPILED_NODES = {
    "nodes": {
        "scan": {
            "entry": True,
            "prompt": (
                "Run vibecop_check on the files changed in this commit. "
                "Scope the scan to only changed files."
            ),
            "transitions": [
                {"when": "no findings", "goto": "done"},
                {"when": "findings exist", "goto": "create_tasks"},
            ],
        },
        "create_tasks": {
            "prompt": "Create tasks for each finding from the scan.",
            "goto": "done",
        },
        "done": {"terminal": True},
    }
}

PLAYBOOK_WITH_COOLDOWN_MD = """\
---
id: slow-gate
triggers:
  - timer.30m
scope: project
cooldown: 120
---

# Slow Gate

Run a periodic check every 30 minutes.
"""


def _make_provider(responses: list[str]) -> AsyncMock:
    """Create a mock ChatProvider that returns the given text responses in order."""
    provider = AsyncMock()
    provider.model_name = "test-model"

    side_effects = []
    for text in responses:
        resp = ChatResponse(content=[TextBlock(text=text)])
        side_effects.append(resp)

    provider.create_message = AsyncMock(side_effect=side_effects)
    return provider


def _wrap_json(data: dict) -> str:
    """Wrap a dict as a fenced JSON code block (the expected LLM output format)."""
    return f"```json\n{json.dumps(data, indent=2)}\n```"


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_basic_frontmatter(self):
        fm, body = PlaybookCompiler._parse_frontmatter(SIMPLE_PLAYBOOK_MD)
        assert fm["id"] == "code-quality-gate"
        assert fm["triggers"] == ["git.commit"]
        assert fm["scope"] == "system"
        assert "Code Quality Gate" in body

    def test_no_frontmatter(self):
        content = "# Just a heading\n\nSome text."
        fm, body = PlaybookCompiler._parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_malformed_yaml(self):
        content = "---\n: invalid: yaml: [[\n---\nBody text"
        fm, body = PlaybookCompiler._parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_incomplete_frontmatter(self):
        content = "---\nid: test\n"
        fm, body = PlaybookCompiler._parse_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_empty_frontmatter(self):
        content = "---\n---\nBody"
        fm, body = PlaybookCompiler._parse_frontmatter(content)
        assert fm == {}
        assert "Body" in body

    def test_frontmatter_with_agent_type_scope(self):
        content = (
            "---\nid: test\ntriggers:\n  - task.completed\nscope: agent-type:coding\n---\nBody"
        )
        fm, body = PlaybookCompiler._parse_frontmatter(content)
        assert fm["scope"] == "agent-type:coding"


# ---------------------------------------------------------------------------
# Frontmatter validation
# ---------------------------------------------------------------------------


class TestValidateFrontmatter:
    def test_valid_frontmatter(self):
        fm = {"id": "test", "triggers": ["git.commit"], "scope": "system"}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert errors == []

    def test_empty_frontmatter(self):
        errors = PlaybookCompiler._validate_frontmatter({})
        assert len(errors) == 1
        assert "Missing YAML frontmatter" in errors[0]

    def test_missing_id(self):
        fm = {"triggers": ["git.commit"], "scope": "system"}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert any("'id'" in e for e in errors)

    def test_missing_triggers(self):
        fm = {"id": "test", "scope": "system"}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert any("'triggers'" in e for e in errors)

    def test_triggers_not_list(self):
        fm = {"id": "test", "triggers": "git.commit", "scope": "system"}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert any("must be a list" in e for e in errors)

    def test_triggers_empty_strings(self):
        fm = {"id": "test", "triggers": ["", "git.commit"], "scope": "system"}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert any("non-empty strings" in e for e in errors)

    def test_missing_scope(self):
        fm = {"id": "test", "triggers": ["git.commit"]}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert any("'scope'" in e for e in errors)

    def test_invalid_scope(self):
        fm = {"id": "test", "triggers": ["git.commit"], "scope": "invalid"}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert any("must be 'system'" in e for e in errors)

    def test_valid_agent_type_scope(self):
        fm = {"id": "test", "triggers": ["git.commit"], "scope": "agent-type:coding"}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert errors == []

    def test_valid_project_scope(self):
        fm = {"id": "test", "triggers": ["git.commit"], "scope": "project"}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert errors == []

    def test_enabled_must_be_bool(self):
        fm = {"id": "test", "triggers": ["git.commit"], "scope": "system", "enabled": "yes"}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert any("boolean" in e for e in errors)

    def test_enabled_true_valid(self):
        fm = {"id": "test", "triggers": ["git.commit"], "scope": "system", "enabled": True}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert errors == []

    def test_multiple_errors_reported(self):
        fm = {"triggers": "not-a-list"}
        errors = PlaybookCompiler._validate_frontmatter(fm)
        assert len(errors) >= 3  # missing id, triggers not list, missing scope


# ---------------------------------------------------------------------------
# Source hash
# ---------------------------------------------------------------------------


class TestSourceHash:
    def test_deterministic(self):
        h1 = PlaybookCompiler._compute_source_hash(SIMPLE_PLAYBOOK_MD)
        h2 = PlaybookCompiler._compute_source_hash(SIMPLE_PLAYBOOK_MD)
        assert h1 == h2

    def test_16_hex_chars(self):
        h = PlaybookCompiler._compute_source_hash(SIMPLE_PLAYBOOK_MD)
        assert len(h) == 16
        assert all(c in "0123456789abcdef" for c in h)

    def test_changes_with_content(self):
        h1 = PlaybookCompiler._compute_source_hash("content A")
        h2 = PlaybookCompiler._compute_source_hash("content B")
        assert h1 != h2

    def test_matches_sha256_of_normalized(self):
        content = "test content"
        normalized = PlaybookCompiler._normalize_content(content)
        expected = hashlib.sha256(normalized.encode()).hexdigest()[:16]
        assert PlaybookCompiler._compute_source_hash(content) == expected


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


class TestExtractJson:
    def test_fenced_json_block(self):
        text = 'Here is the output:\n```json\n{"nodes": {}}\n```\nDone.'
        result = PlaybookCompiler._extract_json(text)
        assert result == {"nodes": {}}

    def test_fenced_code_block_no_lang(self):
        text = 'Output:\n```\n{"nodes": {"a": 1}}\n```'
        result = PlaybookCompiler._extract_json(text)
        assert result == {"nodes": {"a": 1}}

    def test_bare_json(self):
        text = 'The compiled JSON is: {"nodes": {"done": {"terminal": true}}}'
        result = PlaybookCompiler._extract_json(text)
        assert result == {"nodes": {"done": {"terminal": True}}}

    def test_multiline_fenced_json(self):
        data = {"nodes": {"start": {"entry": True, "prompt": "Do thing", "goto": "end"}}}
        text = f"```json\n{json.dumps(data, indent=2)}\n```"
        result = PlaybookCompiler._extract_json(text)
        assert result == data

    def test_no_json(self):
        text = "I cannot compile this playbook because it is unclear."
        result = PlaybookCompiler._extract_json(text)
        assert result is None

    def test_invalid_json_in_fence(self):
        text = "```json\n{invalid json\n```"
        # Falls through to bare JSON strategy
        result = PlaybookCompiler._extract_json(text)
        assert result is None

    def test_nested_braces_bare(self):
        data = {
            "nodes": {
                "a": {"prompt": "test {value}", "entry": True, "goto": "b"},
                "b": {"terminal": True},
            }
        }
        text = f"Output: {json.dumps(data)}"
        result = PlaybookCompiler._extract_json(text)
        assert result == data

    def test_prefers_fenced_over_bare(self):
        """Fenced block takes priority even if bare JSON would also match."""
        fenced = {"source": "fenced"}
        bare = {"source": "bare"}
        text = f"{json.dumps(bare)}\n```json\n{json.dumps(fenced)}\n```"
        result = PlaybookCompiler._extract_json(text)
        assert result == fenced


# ---------------------------------------------------------------------------
# Frontmatter merging
# ---------------------------------------------------------------------------


class TestMergeFrontmatter:
    def test_injects_required_fields(self):
        compiled = {
            "nodes": {"a": {"entry": True, "prompt": "x", "goto": "b"}, "b": {"terminal": True}}
        }
        fm = {"id": "my-pb", "triggers": ["e1"], "scope": "system"}
        result = PlaybookCompiler._merge_frontmatter(compiled, fm, "abc123", 3)

        assert result["id"] == "my-pb"
        assert result["triggers"] == ["e1"]
        assert result["scope"] == "system"
        assert result["source_hash"] == "abc123"
        assert result["version"] == 3
        assert result["nodes"] == compiled["nodes"]

    def test_overwrites_llm_provided_fields(self):
        """Frontmatter is authoritative — LLM values are overwritten."""
        compiled = {
            "id": "wrong-id",
            "triggers": ["wrong"],
            "scope": "wrong",
            "source_hash": "wrong",
            "version": 999,
            "nodes": {},
        }
        fm = {"id": "correct-id", "triggers": ["correct"], "scope": "project"}
        result = PlaybookCompiler._merge_frontmatter(compiled, fm, "hash", 1)

        assert result["id"] == "correct-id"
        assert result["triggers"] == ["correct"]
        assert result["scope"] == "project"
        assert result["source_hash"] == "hash"
        assert result["version"] == 1

    def test_cooldown_from_frontmatter(self):
        compiled = {"nodes": {}}
        fm = {"id": "x", "triggers": ["e"], "scope": "system", "cooldown": 120}
        result = PlaybookCompiler._merge_frontmatter(compiled, fm, "h", 1)
        assert result["cooldown_seconds"] == 120

    def test_no_cooldown_without_frontmatter(self):
        compiled = {"nodes": {}}
        fm = {"id": "x", "triggers": ["e"], "scope": "system"}
        result = PlaybookCompiler._merge_frontmatter(compiled, fm, "h", 1)
        assert "cooldown_seconds" not in result

    def test_preserves_extra_compiled_fields(self):
        """Fields the LLM adds (like max_tokens, llm_config) are preserved."""
        compiled = {"nodes": {}, "max_tokens": 50000}
        fm = {"id": "x", "triggers": ["e"], "scope": "system"}
        result = PlaybookCompiler._merge_frontmatter(compiled, fm, "h", 1)
        assert result["max_tokens"] == 50000

    def test_does_not_mutate_input(self):
        compiled = {"nodes": {}}
        fm = {"id": "x", "triggers": ["e"], "scope": "system"}
        original_compiled = dict(compiled)
        PlaybookCompiler._merge_frontmatter(compiled, fm, "h", 1)
        assert compiled == original_compiled

    def test_llm_config_from_frontmatter(self):
        """llm_config in frontmatter is authoritative and overrides LLM output."""
        compiled = {"nodes": {}, "llm_config": {"model": "wrong"}}
        fm = {
            "id": "x",
            "triggers": ["e"],
            "scope": "system",
            "llm_config": {"provider": "anthropic", "model": "sonnet"},
        }
        result = PlaybookCompiler._merge_frontmatter(compiled, fm, "h", 1)
        assert result["llm_config"] == {"provider": "anthropic", "model": "sonnet"}

    def test_transition_llm_config_from_frontmatter(self):
        """transition_llm_config from frontmatter is injected."""
        compiled = {"nodes": {}}
        fm = {
            "id": "x",
            "triggers": ["e"],
            "scope": "system",
            "transition_llm_config": {"model": "haiku"},
        }
        result = PlaybookCompiler._merge_frontmatter(compiled, fm, "h", 1)
        assert result["transition_llm_config"] == {"model": "haiku"}

    def test_max_tokens_from_frontmatter(self):
        """max_tokens in frontmatter overrides LLM output."""
        compiled = {"nodes": {}, "max_tokens": 99999}
        fm = {
            "id": "x",
            "triggers": ["e"],
            "scope": "system",
            "max_tokens": 50000,
        }
        result = PlaybookCompiler._merge_frontmatter(compiled, fm, "h", 1)
        assert result["max_tokens"] == 50000

    def test_no_llm_config_without_frontmatter(self):
        """LLM-generated llm_config is preserved when frontmatter doesn't override."""
        compiled = {"nodes": {}, "llm_config": {"model": "from-llm"}}
        fm = {"id": "x", "triggers": ["e"], "scope": "system"}
        result = PlaybookCompiler._merge_frontmatter(compiled, fm, "h", 1)
        assert result["llm_config"] == {"model": "from-llm"}

    def test_invalid_llm_config_type_ignored(self):
        """Non-dict llm_config in frontmatter is ignored."""
        compiled = {"nodes": {}}
        fm = {
            "id": "x",
            "triggers": ["e"],
            "scope": "system",
            "llm_config": "not-a-dict",
        }
        result = PlaybookCompiler._merge_frontmatter(compiled, fm, "h", 1)
        assert "llm_config" not in result


# ---------------------------------------------------------------------------
# System / user prompt construction
# ---------------------------------------------------------------------------


class TestPromptConstruction:
    def test_system_prompt_includes_schema(self):
        provider = _make_provider([])
        compiler = PlaybookCompiler(provider)
        prompt = compiler._build_system_prompt()
        assert "JSON Schema" in prompt
        assert '"nodes"' in prompt
        assert '"transition"' in prompt
        assert "entry" in prompt
        assert "terminal" in prompt

    def test_user_message_includes_metadata_and_body(self):
        fm = {"id": "test-pb", "triggers": ["git.commit"], "scope": "system"}
        body = "\n# Test Playbook\n\nDo the thing.\n"
        msg = PlaybookCompiler._build_user_message(fm, body)
        assert "test-pb" in msg
        assert "git.commit" in msg
        assert "system" in msg
        assert "Do the thing" in msg

    def test_user_message_includes_cooldown(self):
        fm = {"id": "x", "triggers": ["e"], "scope": "system", "cooldown": 60}
        msg = PlaybookCompiler._build_user_message(fm, "body")
        assert "60" in msg
        assert "cooldown" in msg.lower()

    def test_retry_message_lists_errors(self):
        errors = ["No entry node found", "Unreachable nodes: ['orphan']"]
        msg = PlaybookCompiler._build_retry_message(errors)
        assert "No entry node found" in msg
        assert "orphan" in msg
        assert "fix" in msg.lower()


# ---------------------------------------------------------------------------
# Full compilation — success path
# ---------------------------------------------------------------------------


class TestCompileSuccess:
    @pytest.mark.asyncio
    async def test_basic_compilation(self):
        """Happy path: LLM returns valid JSON on first attempt."""
        response_json = _wrap_json(VALID_COMPILED_NODES)
        provider = _make_provider([response_json])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)

        assert result.success is True
        assert result.playbook is not None
        assert result.playbook.id == "code-quality-gate"
        assert result.playbook.triggers == ["git.commit"]
        assert result.playbook.scope == "system"
        assert result.playbook.version == 1
        assert len(result.playbook.nodes) == 3
        assert result.retries_used == 0
        assert result.source_hash
        assert len(result.source_hash) == 16

    @pytest.mark.asyncio
    async def test_version_incrementing(self):
        """existing_version is incremented by 1."""
        response_json = _wrap_json(VALID_COMPILED_NODES)
        provider = _make_provider([response_json])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD, existing_version=5)
        assert result.playbook.version == 6

    @pytest.mark.asyncio
    async def test_cooldown_from_frontmatter(self):
        """Cooldown from frontmatter is applied to compiled playbook."""
        response_json = _wrap_json(VALID_COMPILED_NODES)
        provider = _make_provider([response_json])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(PLAYBOOK_WITH_COOLDOWN_MD)
        assert result.success is True
        assert result.playbook.cooldown_seconds == 120
        assert result.playbook.scope == "project"

    @pytest.mark.asyncio
    async def test_frontmatter_overrides_llm_output(self):
        """Even if the LLM includes id/scope/triggers, frontmatter wins."""
        nodes_with_id = {
            "id": "wrong-id",
            "triggers": ["wrong.event"],
            "scope": "project",
            **VALID_COMPILED_NODES,
        }
        response_json = _wrap_json(nodes_with_id)
        provider = _make_provider([response_json])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)
        assert result.success is True
        assert result.playbook.id == "code-quality-gate"
        assert result.playbook.triggers == ["git.commit"]
        assert result.playbook.scope == "system"

    @pytest.mark.asyncio
    async def test_source_hash_in_result(self):
        """Result includes the computed source hash."""
        response_json = _wrap_json(VALID_COMPILED_NODES)
        provider = _make_provider([response_json])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)
        assert result.source_hash
        assert result.playbook.source_hash == result.source_hash

    @pytest.mark.asyncio
    async def test_raw_json_in_result(self):
        """Result includes the raw JSON dict."""
        response_json = _wrap_json(VALID_COMPILED_NODES)
        provider = _make_provider([response_json])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)
        assert result.raw_json is not None
        assert "nodes" in result.raw_json

    @pytest.mark.asyncio
    async def test_provider_called_with_correct_structure(self):
        """Verify the provider receives system + user messages."""
        response_json = _wrap_json(VALID_COMPILED_NODES)
        provider = _make_provider([response_json])
        compiler = PlaybookCompiler(provider)

        await compiler.compile(SIMPLE_PLAYBOOK_MD)

        call = provider.create_message.call_args
        assert call.kwargs["system"]  # system prompt is non-empty
        assert len(call.kwargs["messages"]) == 1
        assert call.kwargs["messages"][0]["role"] == "user"
        assert call.kwargs["max_tokens"] == 4096  # default


# ---------------------------------------------------------------------------
# Full compilation — retry path
# ---------------------------------------------------------------------------


class TestCompileRetry:
    @pytest.mark.asyncio
    async def test_retry_on_missing_entry_node(self):
        """First response missing entry node; second attempt fixes it."""
        # First response: no entry node
        bad_nodes = {
            "nodes": {
                "scan": {
                    "prompt": "Run scan.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            }
        }
        # Second response: fixed with entry=True
        good_nodes = {
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": "Run scan.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            }
        }
        provider = _make_provider([_wrap_json(bad_nodes), _wrap_json(good_nodes)])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)

        assert result.success is True
        assert result.retries_used == 1
        # The retry message should include the validation error
        second_call = provider.create_message.call_args_list[1]
        messages = second_call.kwargs["messages"]
        assert len(messages) == 3  # user + assistant + user (retry feedback)
        assert "entry" in messages[2]["content"].lower()

    @pytest.mark.asyncio
    async def test_retry_exhausted(self):
        """All attempts produce invalid output — compilation fails."""
        bad_nodes = {
            "nodes": {
                "scan": {
                    "prompt": "Run scan.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            }
        }
        # 3 identical bad responses (1 initial + 2 retries)
        provider = _make_provider([_wrap_json(bad_nodes)] * 3)
        compiler = PlaybookCompiler(provider, max_retries=2)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)

        assert result.success is False
        assert len(result.errors) > 0
        assert result.retries_used == 2

    @pytest.mark.asyncio
    async def test_retry_on_no_json(self):
        """First response has no JSON; second attempt includes it."""
        provider = _make_provider(
            [
                "I'll think about this...",
                _wrap_json(VALID_COMPILED_NODES),
            ]
        )
        compiler = PlaybookCompiler(provider, max_retries=1)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)
        assert result.success is True
        assert result.retries_used == 1

    @pytest.mark.asyncio
    async def test_zero_retries(self):
        """With max_retries=0, only one attempt is made."""
        bad_nodes = {"nodes": {"a": {"prompt": "x", "goto": "b"}, "b": {"terminal": True}}}
        provider = _make_provider([_wrap_json(bad_nodes)])
        compiler = PlaybookCompiler(provider, max_retries=0)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)
        assert result.success is False
        assert provider.create_message.call_count == 1


# ---------------------------------------------------------------------------
# Full compilation — error paths
# ---------------------------------------------------------------------------


class TestCompileErrors:
    @pytest.mark.asyncio
    async def test_missing_frontmatter(self):
        """Markdown without frontmatter fails immediately (no LLM call)."""
        md = "# Just a heading\n\nSome process description."
        provider = _make_provider([])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(md)

        assert result.success is False
        assert any("frontmatter" in e.lower() for e in result.errors)
        provider.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_required_frontmatter_fields(self):
        """Frontmatter missing required fields fails before LLM call."""
        md = "---\nid: test\n---\nBody"
        provider = _make_provider([])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(md)

        assert result.success is False
        assert any("triggers" in e for e in result.errors)
        provider.create_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_call_failure(self):
        """LLM provider raising an exception is handled gracefully."""
        provider = AsyncMock()
        provider.model_name = "test-model"
        provider.create_message = AsyncMock(side_effect=RuntimeError("API timeout"))

        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)

        assert result.success is False
        assert any("LLM call failed" in e for e in result.errors)
        assert result.source_hash  # hash is still computed

    @pytest.mark.asyncio
    async def test_deserialization_failure(self):
        """JSON that can't be deserialized into CompiledPlaybook."""
        # Valid JSON but wrong structure for from_dict (missing "id" after merge
        # won't happen, but missing nodes structure can break things)
        # Actually from_dict is quite lenient, so we need something that raises
        bad_json = {"nodes": {"a": {"transitions": "not-a-list"}}}
        provider = _make_provider([_wrap_json(bad_json)] * 3)
        compiler = PlaybookCompiler(provider, max_retries=2)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)

        # Should fail at deserialization or validation
        assert result.success is False


# ---------------------------------------------------------------------------
# CompilationResult
# ---------------------------------------------------------------------------


class TestCompilationResult:
    def test_success_result(self):
        pb = CompiledPlaybook(
            id="test",
            version=1,
            source_hash="abc",
            triggers=["e"],
            scope="system",
            nodes={},
        )
        result = CompilationResult(success=True, playbook=pb, source_hash="abc")
        assert result.success
        assert result.playbook is pb
        assert result.errors == []
        assert result.retries_used == 0

    def test_failure_result(self):
        result = CompilationResult(
            success=False,
            errors=["error 1", "error 2"],
            source_hash="def",
            retries_used=2,
        )
        assert not result.success
        assert result.playbook is None
        assert len(result.errors) == 2
        assert result.retries_used == 2


# ---------------------------------------------------------------------------
# Integration-style: round-trip with spec example
# ---------------------------------------------------------------------------


class TestSpecExampleRoundTrip:
    """Test compilation with the spec §5 example output."""

    @pytest.mark.asyncio
    async def test_spec_example_compiles(self):
        """The spec §5 example JSON (as LLM output) should validate."""
        spec_nodes = {
            "nodes": {
                "scan": {
                    "entry": True,
                    "prompt": (
                        "Run vibecop_check on the files changed in this commit. "
                        "Use the diff to scope the scan to only changed files, "
                        "not the entire repo."
                    ),
                    "transitions": [
                        {"when": "no findings", "goto": "done"},
                        {"when": "findings exist", "goto": "triage"},
                    ],
                },
                "triage": {
                    "prompt": ("Group the scan findings by severity (error, warning, info)."),
                    "transitions": [
                        {"when": "has errors", "goto": "create_error_tasks"},
                        {"when": "warnings only", "goto": "create_warning_task"},
                        {"when": "info only", "goto": "log_to_memory"},
                    ],
                },
                "create_error_tasks": {
                    "prompt": (
                        "Create one high-priority task per file that has errors. "
                        "Include the vibecop output and file path."
                    ),
                    "goto": "create_warning_task",
                },
                "create_warning_task": {
                    "prompt": "Batch all warnings into a single medium-priority task.",
                    "goto": "log_to_memory",
                },
                "log_to_memory": {
                    "prompt": "Record any info-level findings in project memory.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            },
            "cooldown_seconds": 60,
            "max_tokens": 50000,
        }

        provider = _make_provider([_wrap_json(spec_nodes)])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)

        assert result.success is True
        pb = result.playbook
        assert pb.id == "code-quality-gate"
        assert len(pb.nodes) == 6
        assert pb.entry_node_id() == "scan"
        assert pb.terminal_node_ids() == ["done"]
        assert pb.cooldown_seconds == 60
        assert pb.max_tokens == 50000

    @pytest.mark.asyncio
    async def test_complex_playbook_with_llm_config(self):
        """Playbook with per-node llm_config compiles correctly."""
        nodes = {
            "nodes": {
                "analyze": {
                    "entry": True,
                    "prompt": "Analyze the issue.",
                    "llm_config": {"provider": "anthropic", "model": "claude-opus-4"},
                    "transitions": [
                        {"when": "feasible", "goto": "plan"},
                        {"otherwise": True, "goto": "reject"},
                    ],
                },
                "plan": {
                    "prompt": "Draft an implementation plan.",
                    "wait_for_human": True,
                    "goto": "done",
                },
                "reject": {
                    "prompt": "Explain why this is not feasible.",
                    "goto": "done",
                },
                "done": {"terminal": True},
            }
        }

        provider = _make_provider([_wrap_json(nodes)])
        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)

        assert result.success is True
        analyze = result.playbook.nodes["analyze"]
        assert analyze.llm_config is not None
        assert analyze.llm_config.model == "claude-opus-4"
        assert result.playbook.nodes["plan"].wait_for_human is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_bare_json_response(self):
        """LLM returns JSON without code fences — still works."""
        raw = json.dumps(VALID_COMPILED_NODES)
        provider = _make_provider([raw])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_json_with_surrounding_text(self):
        """LLM returns JSON with explanation text around it."""
        response = (
            "Here is the compiled playbook:\n\n"
            f"```json\n{json.dumps(VALID_COMPILED_NODES, indent=2)}\n```\n\n"
            "This covers all the steps described in the markdown."
        )
        provider = _make_provider([response])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_custom_max_tokens(self):
        """Custom max_tokens is passed through to the provider."""
        response_json = _wrap_json(VALID_COMPILED_NODES)
        provider = _make_provider([response_json])
        compiler = PlaybookCompiler(provider, max_tokens=8192)

        await compiler.compile(SIMPLE_PLAYBOOK_MD)

        call = provider.create_message.call_args
        assert call.kwargs["max_tokens"] == 8192

    @pytest.mark.asyncio
    async def test_enabled_false_still_compiles(self):
        """enabled: false in frontmatter doesn't block compilation."""
        md = "---\nid: test\ntriggers:\n  - e\nscope: system\nenabled: false\n---\nBody"
        response_json = _wrap_json(VALID_COMPILED_NODES)
        provider = _make_provider([response_json])
        compiler = PlaybookCompiler(provider)

        result = await compiler.compile(md)
        assert result.success is True


# ---------------------------------------------------------------------------
# Compilation happy-path — roadmap 5.1.8 (a)-(g)
# ---------------------------------------------------------------------------

# A richer 3-node playbook markdown used throughout this section.
# Describes a code-quality-gate with scan → create_tasks → done.
HAPPY_PATH_MD = """\
---
id: code-quality-gate
triggers:
  - git.commit
  - git.push
scope: project
cooldown: 60
---

# Code Quality Gate

When code is committed, run vibecop on the changed files.
If issues are found, create tasks to fix them. Otherwise, we're done.
"""

# The LLM response for the happy-path 3-node playbook.  Includes a rich
# set of node/transition fields exercised by tests (c) and (d).
HAPPY_PATH_LLM_NODES: dict = {
    "nodes": {
        "scan": {
            "entry": True,
            "prompt": (
                "Run vibecop_check on the files changed in this commit. "
                "Scope the scan to only the changed files."
            ),
            "summarize_before": True,
            "llm_config": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
            "transitions": [
                {"when": "no findings", "goto": "done"},
                {"when": "findings exist", "goto": "create_tasks"},
                {
                    "when": {
                        "function": "has_tool_output",
                        "contains": "critical",
                    },
                    "goto": "create_tasks",
                },
                {"otherwise": True, "goto": "done"},
            ],
        },
        "create_tasks": {
            "prompt": "Create one task per finding from the scan.",
            "timeout_seconds": 120,
            "goto": "done",
        },
        "done": {"terminal": True},
    },
    "max_tokens": 50000,
}


class _FakeVaultManager:
    """Minimal VaultManager stub — only ``compiled_root`` is needed."""

    def __init__(self, compiled_root: str) -> None:
        self._compiled_root = compiled_root

    @property
    def compiled_root(self) -> str:
        return self._compiled_root


class TestCompilationHappyPath:
    """Roadmap 5.1.8 — compilation happy-path tests (a)-(g).

    All tests use a sample 3-node playbook markdown that is compiled via a
    mocked LLM provider and then verified against the spec expectations from
    playbooks.md §4 (Authoring Model) and §5 (Compiled Format).
    """

    # -- (a) Schema validation ------------------------------------------------

    @pytest.mark.asyncio
    async def test_a_three_node_playbook_validates_against_schema(self):
        """(a) Sample 3-node playbook compiles to JSON validating against the schema."""
        import jsonschema

        provider = _make_provider([_wrap_json(HAPPY_PATH_LLM_NODES)])
        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(HAPPY_PATH_MD)

        assert result.success is True
        assert result.playbook is not None

        # Serialize to dict and validate against the generated JSON Schema
        compiled_dict = result.playbook.to_dict()
        schema = generate_json_schema()
        jsonschema.validate(instance=compiled_dict, schema=schema)

        # Additional: CompiledPlaybook's own structural validation passes
        assert result.playbook.validate() == []

    # -- (b) Entry node, node definitions, transitions -----------------------

    @pytest.mark.asyncio
    async def test_b_entry_node_all_nodes_and_transitions(self):
        """(b) Compiled JSON has correct entry_node, all node defs, all transitions."""
        provider = _make_provider([_wrap_json(HAPPY_PATH_LLM_NODES)])
        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(HAPPY_PATH_MD)

        pb = result.playbook
        assert pb is not None

        # Correct entry node
        assert pb.entry_node_id() == "scan"

        # All three node definitions present
        assert set(pb.nodes.keys()) == {"scan", "create_tasks", "done"}

        # Terminal nodes
        assert pb.terminal_node_ids() == ["done"]

        # Scan node transitions
        scan = pb.nodes["scan"]
        assert len(scan.transitions) == 4
        assert scan.transitions[0].goto == "done"
        assert scan.transitions[0].when == "no findings"
        assert scan.transitions[1].goto == "create_tasks"
        assert scan.transitions[1].when == "findings exist"
        # Structured transition
        assert isinstance(scan.transitions[2].when, dict)
        assert scan.transitions[2].goto == "create_tasks"
        # Otherwise fallback
        assert scan.transitions[3].otherwise is True
        assert scan.transitions[3].goto == "done"

        # create_tasks node uses unconditional goto (not transitions)
        ct = pb.nodes["create_tasks"]
        assert ct.goto == "done"
        assert ct.transitions == []

        # done is a bare terminal
        done = pb.nodes["done"]
        assert done.terminal is True
        assert done.transitions == []
        assert done.goto is None

        # All nodes reachable, all reach terminal
        assert pb.reachable_node_ids() == {"scan", "create_tasks", "done"}

    # -- (c) Node fields -----------------------------------------------------

    @pytest.mark.asyncio
    async def test_c_node_fields_correctly_extracted(self):
        """(c) Node fields (prompt, llm_config, summarize_before) extracted correctly.

        Note: ``tools`` is listed in the roadmap but is not yet a PlaybookNode
        field — it is expected to be added in a future spec revision.  All
        currently-defined node fields are tested here.
        """
        provider = _make_provider([_wrap_json(HAPPY_PATH_LLM_NODES)])
        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(HAPPY_PATH_MD)

        pb = result.playbook
        assert pb is not None

        # scan node: prompt, llm_config, summarize_before
        scan = pb.nodes["scan"]
        assert scan.prompt.startswith("Run vibecop_check")
        assert scan.entry is True
        assert scan.terminal is False
        assert scan.summarize_before is True
        assert scan.llm_config is not None
        assert scan.llm_config.provider == "anthropic"
        assert scan.llm_config.model == "claude-sonnet-4-20250514"

        # create_tasks node: prompt, timeout_seconds
        ct = pb.nodes["create_tasks"]
        assert ct.prompt == "Create one task per finding from the scan."
        assert ct.timeout_seconds == 120
        assert ct.entry is False
        assert ct.terminal is False
        assert ct.summarize_before is False
        assert ct.llm_config is None

        # done node: terminal, no prompt required
        done = pb.nodes["done"]
        assert done.terminal is True
        assert done.prompt == ""
        assert done.wait_for_human is False
        assert done.llm_config is None
        assert done.summarize_before is False

    # -- (d) Transition fields -----------------------------------------------

    @pytest.mark.asyncio
    async def test_d_transition_fields_correctly_extracted(self):
        """(d) Transition fields (condition, target, structured expr) extracted correctly."""
        provider = _make_provider([_wrap_json(HAPPY_PATH_LLM_NODES)])
        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(HAPPY_PATH_MD)

        pb = result.playbook
        assert pb is not None
        transitions = pb.nodes["scan"].transitions

        # Natural-language condition + target
        t0 = transitions[0]
        assert t0.when == "no findings"
        assert t0.goto == "done"
        assert t0.otherwise is False

        t1 = transitions[1]
        assert t1.when == "findings exist"
        assert t1.goto == "create_tasks"
        assert t1.otherwise is False

        # Structured expression (dict-based condition)
        t2 = transitions[2]
        assert isinstance(t2.when, dict)
        assert t2.when["function"] == "has_tool_output"
        assert t2.when["contains"] == "critical"
        assert t2.goto == "create_tasks"
        assert t2.otherwise is False

        # Otherwise fallback
        t3 = transitions[3]
        assert t3.when is None
        assert t3.otherwise is True
        assert t3.goto == "done"

        # Unconditional goto (create_tasks → done)
        assert pb.nodes["create_tasks"].goto == "done"
        assert pb.nodes["create_tasks"].transitions == []

    # -- (e) Frontmatter fields preserved ------------------------------------

    @pytest.mark.asyncio
    async def test_e_frontmatter_fields_preserved(self):
        """(e) Frontmatter fields (trigger, scope, cooldown) preserved in compiled output."""
        provider = _make_provider([_wrap_json(HAPPY_PATH_LLM_NODES)])
        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(HAPPY_PATH_MD)

        pb = result.playbook
        assert pb is not None

        # triggers from frontmatter
        assert pb.triggers == ["git.commit", "git.push"]

        # scope from frontmatter
        assert pb.scope == "project"

        # cooldown from frontmatter (mapped to cooldown_seconds)
        assert pb.cooldown_seconds == 60

        # id from frontmatter
        assert pb.id == "code-quality-gate"

        # version defaults to 1 (existing_version=0 + 1)
        assert pb.version == 1

        # source_hash comes from the compiler, not frontmatter
        expected_hash = PlaybookCompiler._compute_source_hash(HAPPY_PATH_MD)
        assert pb.source_hash == expected_hash

    @pytest.mark.asyncio
    async def test_e_frontmatter_overrides_llm_duplicates(self):
        """(e) Even if the LLM echoes frontmatter fields, they are overwritten."""
        nodes_with_llm_metadata = {
            "id": "wrong-id",
            "triggers": ["wrong.event"],
            "scope": "system",
            "source_hash": "0000000000000000",
            "version": 999,
            **HAPPY_PATH_LLM_NODES,
        }
        provider = _make_provider([_wrap_json(nodes_with_llm_metadata)])
        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(HAPPY_PATH_MD)

        pb = result.playbook
        assert pb is not None
        # Frontmatter always wins
        assert pb.id == "code-quality-gate"
        assert pb.triggers == ["git.commit", "git.push"]
        assert pb.scope == "project"
        assert pb.cooldown_seconds == 60
        assert pb.version == 1
        assert pb.source_hash != "0000000000000000"

    # -- (f) Idempotency ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_f_compilation_idempotent(self):
        """(f) Compiling the same markdown twice produces identical JSON."""
        # Create two independent providers that return the same response
        response_text = _wrap_json(HAPPY_PATH_LLM_NODES)

        provider1 = _make_provider([response_text])
        compiler1 = PlaybookCompiler(provider1)
        result1 = await compiler1.compile(HAPPY_PATH_MD)

        provider2 = _make_provider([response_text])
        compiler2 = PlaybookCompiler(provider2)
        result2 = await compiler2.compile(HAPPY_PATH_MD)

        assert result1.success is True
        assert result2.success is True

        # Serialize both to dicts and compare (excluding compiled_at which
        # is a timestamp and will differ between compilations)
        dict1 = result1.playbook.to_dict()
        dict2 = result2.playbook.to_dict()
        dict1.pop("compiled_at", None)
        dict2.pop("compiled_at", None)
        assert dict1 == dict2

        # Also compare source hashes
        assert result1.source_hash == result2.source_hash

        # And JSON serializations are byte-identical (excluding timestamp)
        json1 = json.dumps(dict1, sort_keys=True)
        json2 = json.dumps(dict2, sort_keys=True)
        assert json1 == json2

    @pytest.mark.asyncio
    async def test_f_idempotent_with_same_existing_version(self):
        """(f) Same markdown + same existing_version → identical output."""
        response_text = _wrap_json(HAPPY_PATH_LLM_NODES)

        provider1 = _make_provider([response_text])
        compiler1 = PlaybookCompiler(provider1)
        result1 = await compiler1.compile(HAPPY_PATH_MD, existing_version=3)

        provider2 = _make_provider([response_text])
        compiler2 = PlaybookCompiler(provider2)
        result2 = await compiler2.compile(HAPPY_PATH_MD, existing_version=3)

        assert result1.playbook.version == 4
        assert result2.playbook.version == 4
        d1 = result1.playbook.to_dict()
        d2 = result2.playbook.to_dict()
        d1.pop("compiled_at", None)
        d2.pop("compiled_at", None)
        assert d1 == d2

    # -- (g) Storage at correct path ----------------------------------------

    @pytest.mark.asyncio
    async def test_g_stored_at_correct_system_scope_path(self, tmp_path):
        """(g) Compiled JSON stored at correct path for system scope."""
        provider = _make_provider([_wrap_json(VALID_COMPILED_NODES)])
        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(SIMPLE_PLAYBOOK_MD)

        assert result.success is True

        # Store via CompiledPlaybookStore
        compiled_root = str(tmp_path / "compiled")
        vm = _FakeVaultManager(compiled_root)
        store = CompiledPlaybookStore(vm)

        path = store.save(result.playbook, "system")

        # Path mirrors scope: compiled/system/{id}.compiled.json
        expected = os.path.join(compiled_root, "system", "code-quality-gate.compiled.json")
        assert path == expected
        assert os.path.isfile(path)

        # Round-trip: load and verify
        loaded = store.load("code-quality-gate", "system")
        assert loaded is not None
        assert loaded.id == result.playbook.id
        assert loaded.source_hash == result.playbook.source_hash

    @pytest.mark.asyncio
    async def test_g_stored_at_correct_project_scope_path(self, tmp_path):
        """(g) Compiled JSON stored at correct path for project scope."""
        provider = _make_provider([_wrap_json(HAPPY_PATH_LLM_NODES)])
        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(HAPPY_PATH_MD)

        assert result.success is True
        assert result.playbook.scope == "project"

        compiled_root = str(tmp_path / "compiled")
        vm = _FakeVaultManager(compiled_root)
        store = CompiledPlaybookStore(vm)

        path = store.save(result.playbook, "project", "my-app")

        expected = os.path.join(
            compiled_root, "projects", "my-app", "code-quality-gate.compiled.json"
        )
        assert path == expected
        assert os.path.isfile(path)

        # Verify the stored JSON content
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["id"] == "code-quality-gate"
        assert data["scope"] == "project"
        assert data["triggers"] == ["git.commit", "git.push"]
        assert data["cooldown_seconds"] == 60
        assert "scan" in data["nodes"]
        assert "create_tasks" in data["nodes"]
        assert "done" in data["nodes"]

    @pytest.mark.asyncio
    async def test_g_stored_at_correct_agent_type_scope_path(self, tmp_path):
        """(g) Compiled JSON stored at correct path for agent-type scope."""
        md = """\
---
id: lint-check
triggers:
  - task.started
scope: agent-type:coding
---

# Lint Check

Run lint on all changed files when a coding agent starts a task.
"""
        provider = _make_provider([_wrap_json(VALID_COMPILED_NODES)])
        compiler = PlaybookCompiler(provider)
        result = await compiler.compile(md)

        assert result.success is True
        assert result.playbook.scope == "agent-type:coding"

        compiled_root = str(tmp_path / "compiled")
        vm = _FakeVaultManager(compiled_root)
        store = CompiledPlaybookStore(vm)

        path = store.save(result.playbook, "agent_type", "coding")

        expected = os.path.join(compiled_root, "agent-types", "coding", "lint-check.compiled.json")
        assert path == expected
        assert os.path.isfile(path)

        loaded = store.load("lint-check", "agent_type", "coding")
        assert loaded is not None
        assert loaded.scope == "agent-type:coding"
        assert loaded.triggers == ["task.started"]
