"""Harness markdown parsing, inheritance, and registry precedence.

See docs/specs/implementation/session-runtime.md §3.5 and design §6.
"""

from __future__ import annotations

import textwrap

from src.sessions.harness_parser import (
    parse_harness_markdown,
    resolve_base,
)
from src.sessions.harness_registry import (
    HarnessRegistry,
    derive_harness_id,
    load_from_vault,
    vault_path_for,
)


def _md(config_json: str, *, frontmatter: str = "id: claude\nname: Claude Code\n") -> str:
    # Built by concatenation, not textwrap.dedent: interpolation happens
    # before dedent, so an un-indented interpolated block makes the common
    # prefix empty and silently leaves the whole template indented -- which
    # the ``^## Config$`` anchor then fails to match.
    return (
        "---\n"
        + frontmatter
        + "---\n\n## Config\n\n```json\n"
        + textwrap.dedent(config_json).strip()
        + "\n```\n\n## Notes\n\nProse that is never parsed.\n"
    )


class TestBasicParsing:
    def test_minimal_harness(self):
        parsed = parse_harness_markdown(_md('{"command": "claude"}'))
        assert parsed.is_valid, parsed.errors
        h = parsed.harness
        assert h.id == "claude"
        assert h.name == "Claude Code"
        assert h.command == "claude"
        assert h.prompt_mode == "arg"  # shipped default
        assert h.notes.startswith("Prose") or "Prose" in h.notes

    def test_full_field_set_round_trips(self):
        parsed = parse_harness_markdown(
            _md(
                """
                {
                  "command": "claude",
                  "args": ["--verbose"],
                  "prompt_mode": "arg",
                  "permission_flag": "--dangerously-skip-permissions",
                  "model_flag": "--model",
                  "effort_flag": "--effort",
                  "session_id_flag": "--session-id",
                  "resume": {"style": "flag", "flag": "--resume", "supports_fork": true},
                  "ready_delay_ms": 1500,
                  "ready_prompt_prefix": "> ",
                  "process_names": ["claude", "node"],
                  "skip_escape_before_enter": false,
                  "supports_hooks": true,
                  "hook_files": {".aq/hooks/claude.json": "hooks/claude.json"},
                  "instructions_file": "CLAUDE.md",
                  "transcript_paths": ["~/.claude/projects/x/*.jsonl"],
                  "env": {"FOO": "bar"},
                  "max_argv_prompt_bytes": 2048
                }
                """
            )
        )
        assert parsed.is_valid, parsed.errors
        h = parsed.harness
        assert h.args == ("--verbose",)
        assert h.resume.style == "flag" and h.resume.supports_fork is True
        assert h.ready_delay_ms == 1500
        assert h.process_names == ("claude", "node")
        assert h.skip_escape_before_enter is False
        assert h.hook_files == ((".aq/hooks/claude.json", "hooks/claude.json"),)
        assert h.env_map == {"FOO": "bar"}
        assert h.max_argv_prompt_bytes == 2048

    def test_id_falls_back_to_the_filename_stem(self):
        parsed = parse_harness_markdown(
            _md('{"command": "codex"}', frontmatter="name: Codex\n"),
            fallback_id="codex",
        )
        assert parsed.is_valid, parsed.errors
        assert parsed.harness.id == "codex"

    def test_project_scope_is_recorded(self):
        parsed = parse_harness_markdown(_md('{"command": "claude"}'), project_id="proj1")
        assert parsed.harness.project_id == "proj1"
        assert parsed.harness.scope_key == ("proj1", "claude")

    def test_dialogs_parse_into_rules(self):
        parsed = parse_harness_markdown(
            _md(
                """
                {
                  "command": "claude",
                  "dialogs": [
                    {"name": "trust", "pattern": "Do you trust", "keys": ["Enter"]},
                    {"name": "rl", "pattern": "usage limit", "keys": "Escape",
                     "quarantine": true}
                  ]
                }
                """
            )
        )
        assert parsed.is_valid, parsed.errors
        trust, rl = parsed.harness.dialogs
        assert trust.name == "trust" and trust.keys == ("Enter",)
        # A bare string is accepted for `keys` -- one key is the common case.
        assert rl.keys == ("Escape",) and rl.quarantine is True


class TestValidationRefusesRatherThanGuesses:
    def test_missing_config_section(self):
        parsed = parse_harness_markdown("---\nid: x\n---\n\nJust prose.\n")
        assert not parsed.is_valid
        assert any("## Config" in e for e in parsed.errors)

    def test_config_section_without_a_json_block(self):
        parsed = parse_harness_markdown("---\nid: x\n---\n\n## Config\n\nno json here\n")
        assert not parsed.is_valid
        assert any("no JSON code block" in e for e in parsed.errors)

    def test_invalid_json(self):
        parsed = parse_harness_markdown(_md('{"command": "claude",}'))
        assert not parsed.is_valid
        assert any("invalid JSON" in e for e in parsed.errors)

    def test_missing_command_without_base(self):
        parsed = parse_harness_markdown(_md('{"prompt_mode": "arg"}'))
        assert not parsed.is_valid
        assert any("'command' is required" in e for e in parsed.errors)

    def test_invalid_prompt_mode(self):
        parsed = parse_harness_markdown(_md('{"command": "x", "prompt_mode": "stdin"}'))
        assert not parsed.is_valid
        assert any("invalid prompt_mode" in e for e in parsed.errors)

    def test_prompt_mode_flag_requires_a_flag(self):
        parsed = parse_harness_markdown(_md('{"command": "x", "prompt_mode": "flag"}'))
        assert not parsed.is_valid
        assert any("requires 'prompt_flag'" in e for e in parsed.errors)

    def test_invalid_resume_style(self):
        parsed = parse_harness_markdown(
            _md('{"command": "x", "resume": {"style": "magic"}}')
        )
        assert not parsed.is_valid
        assert any("invalid resume.style" in e for e in parsed.errors)

    def test_negative_ready_delay(self):
        parsed = parse_harness_markdown(_md('{"command": "x", "ready_delay_ms": -1}'))
        assert not parsed.is_valid

    def test_dialog_without_a_pattern(self):
        parsed = parse_harness_markdown(
            _md('{"command": "x", "dialogs": [{"name": "n", "keys": ["Enter"]}]}')
        )
        assert not parsed.is_valid
        assert any("'pattern'" in e for e in parsed.errors)

    def test_unknown_key_is_a_warning_not_an_error(self):
        """A file authored against a newer daemon must still load."""
        parsed = parse_harness_markdown(_md('{"command": "x", "future_thing": 1}'))
        assert parsed.is_valid
        assert any("future_thing" in w for w in parsed.warnings)


class TestBaseInheritance:
    def _parse(self, cfg, hid):
        return parse_harness_markdown(
            _md(cfg, frontmatter=f"id: {hid}\n"),
        ).harness

    def test_child_inherits_unset_fields(self):
        parent = self._parse(
            '{"command": "claude", "args": ["--x"], "process_names": ["claude"]}', "claude"
        )
        child = self._parse('{"base": "claude", "model_flag": "--model"}', "claude-fast")
        resolved, errors = resolve_base(child, lambda n: parent if n == "claude" else None)
        assert errors == []
        assert resolved.command == "claude"
        assert resolved.args == ("--x",)
        assert resolved.process_names == ("claude",)
        assert resolved.model_flag == "--model"

    def test_child_overrides_win(self):
        parent = self._parse('{"command": "claude", "args": ["--parent"]}', "claude")
        child = self._parse('{"base": "claude", "args": ["--child"]}', "c2")
        resolved, _ = resolve_base(child, lambda n: parent)
        assert resolved.args == ("--child",)

    def test_env_merges_key_wise(self):
        """Adding one variable must not drop the parent's."""
        parent = self._parse('{"command": "c", "env": {"A": "1", "B": "2"}}', "claude")
        child = self._parse('{"base": "claude", "env": {"B": "9", "C": "3"}}', "c2")
        resolved, _ = resolve_base(child, lambda n: parent)
        assert resolved.env_map == {"A": "1", "B": "9", "C": "3"}

    def test_self_reference_is_rejected(self):
        h = self._parse('{"base": "loop", "command": "x"}', "loop")
        _, errors = resolve_base(h, lambda n: h)
        assert any("its own base" in e for e in errors)

    def test_unknown_base_is_rejected(self):
        child = self._parse('{"base": "ghost", "command": "x"}', "c")
        _, errors = resolve_base(child, lambda n: None)
        assert any("unknown base" in e for e in errors)

    def test_multi_level_chains_are_rejected_not_walked(self):
        """One level deep makes a cycle impossible by construction."""
        grandparent = self._parse('{"command": "g"}', "gp")
        parent = self._parse('{"base": "gp", "command": "p"}', "p")
        child = self._parse('{"base": "p", "command": "c"}', "c")
        lookup = {"gp": grandparent, "p": parent}.get
        _, errors = resolve_base(child, lookup)
        assert any("single-level" in e for e in errors)


class TestPathDerivation:
    def test_system_scope(self):
        assert derive_harness_id("harnesses/claude.md") == (None, "claude")

    def test_project_scope(self):
        assert derive_harness_id("projects/p1/harnesses/claude.md") == ("p1", "claude")

    def test_windows_separators_are_normalized(self):
        assert derive_harness_id("harnesses\\claude.md") == (None, "claude")

    def test_unrelated_paths_return_none(self):
        assert derive_harness_id("mcp-servers/claude.md") is None
        assert derive_harness_id("harnesses/nested/claude.md") is None

    def test_vault_path_for_round_trips(self, tmp_path):
        p = vault_path_for(str(tmp_path), "claude", None)
        assert p.endswith(("vault/harnesses/claude.md", "vault\\harnesses\\claude.md"))
        p = vault_path_for(str(tmp_path), "claude", "proj")
        assert "proj" in p


class TestRegistry:
    def _write(self, root, rel, cfg, hid="claude"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_md(cfg, frontmatter=f"id: {hid}\n"), encoding="utf-8")

    def test_load_from_vault_populates_both_scopes(self, tmp_path):
        vault = tmp_path / "vault"
        self._write(vault, "harnesses/claude.md", '{"command": "claude"}')
        self._write(
            vault, "projects/p1/harnesses/claude.md", '{"command": "claude-custom"}'
        )
        registry = HarnessRegistry()
        errors = load_from_vault(registry, str(vault))
        assert errors == []
        assert len(registry) == 2

    def test_project_scope_shadows_system_scope(self, tmp_path):
        vault = tmp_path / "vault"
        self._write(vault, "harnesses/claude.md", '{"command": "system-claude"}')
        self._write(vault, "projects/p1/harnesses/claude.md", '{"command": "proj-claude"}')
        registry = HarnessRegistry()
        load_from_vault(registry, str(vault))
        assert registry.get("claude").command == "system-claude"
        assert registry.get("claude", "p1").command == "proj-claude"
        # An unrelated project still sees the system entry.
        assert registry.get("claude", "p2").command == "system-claude"

    def test_get_resolves_base_at_lookup_time(self, tmp_path):
        vault = tmp_path / "vault"
        self._write(vault, "harnesses/claude.md", '{"command": "claude"}', hid="claude")
        self._write(
            vault,
            "harnesses/claude-fast.md",
            '{"base": "claude", "model_flag": "--model"}',
            hid="claude-fast",
        )
        registry = HarnessRegistry()
        load_from_vault(registry, str(vault))
        fast = registry.get("claude-fast")
        assert fast.command == "claude"
        assert fast.model_flag == "--model"

    def test_a_malformed_file_is_skipped_and_reported_not_fatal(self, tmp_path):
        vault = tmp_path / "vault"
        self._write(vault, "harnesses/good.md", '{"command": "ok"}', hid="good")
        bad = vault / "harnesses" / "bad.md"
        bad.write_text("---\nid: bad\n---\n\nno config\n", encoding="utf-8")
        registry = HarnessRegistry()
        errors = load_from_vault(registry, str(vault))
        assert len(errors) == 1 and "bad.md" in errors[0]
        assert registry.get("good") is not None
        assert registry.get("bad") is None

    def test_missing_vault_root_is_not_an_error(self, tmp_path):
        registry = HarnessRegistry()
        assert load_from_vault(registry, str(tmp_path / "nope")) == []
        assert len(registry) == 0

    def test_list_for_scope_merges_inherited_system_entries(self, tmp_path):
        vault = tmp_path / "vault"
        self._write(vault, "harnesses/claude.md", '{"command": "c"}', hid="claude")
        self._write(vault, "harnesses/codex.md", '{"command": "x"}', hid="codex")
        self._write(vault, "projects/p1/harnesses/claude.md", '{"command": "c2"}')
        registry = HarnessRegistry()
        load_from_vault(registry, str(vault))
        ids = [h.id for h in registry.list_for_scope("p1")]
        assert sorted(ids) == ["claude", "codex"]
        # The project's claude shadows the system one -- not both.
        assert len([h for h in registry.list_for_scope("p1") if h.id == "claude"]) == 1


class TestShippedClaudeHarness:
    """The bundled ``claude.md`` must parse and carry the load-bearing bits."""

    def test_shipped_default_parses(self, tmp_path):
        from src.vault import ensure_default_harnesses

        result = ensure_default_harnesses(str(tmp_path))
        assert "claude.md" in result["created"]

        registry = HarnessRegistry()
        errors = load_from_vault(registry, str(tmp_path / "vault"))
        assert errors == []
        h = registry.get("claude")
        assert h is not None
        assert h.command == "claude"
        assert h.prompt_mode == "arg"
        assert h.resume.style == "flag"
        assert "claude" in h.process_names
        assert h.supports_hooks is True
        # The chevron is the load-bearing character; the trailing space may
        # be NBSP or plain, because the readiness poll normalizes NBSP
        # before matching.  Pin the chevron so a well-meaning "fix" to the
        # shipped file cannot silently stop readiness from ever matching.
        assert h.ready_prompt_prefix is not None
        assert h.ready_prompt_prefix.startswith("❯")

    def test_seeding_is_idempotent_and_never_overwrites(self, tmp_path):
        from src.vault import ensure_default_harnesses

        ensure_default_harnesses(str(tmp_path))
        target = tmp_path / "vault" / "harnesses" / "claude.md"
        target.write_text("---\nid: claude\n---\n\n## Config\n\n```json\n"
                          '{"command": "my-claude"}\n```\n', encoding="utf-8")
        result = ensure_default_harnesses(str(tmp_path))
        assert result["created"] == []
        assert "claude.md" in result["skipped"]
        assert "my-claude" in target.read_text(encoding="utf-8")
