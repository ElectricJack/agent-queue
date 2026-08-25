"""A profile's ``allowed_tools`` must actually reach the session launch.

Before this, ``allowed_tools`` appeared nowhere in ``src/sessions/`` — the
only mention in the launch path was a *log line* printing it. Every session
agent ran on the CLI's full default toolset regardless of what its profile
declared, so ``reviewer``'s ``read_only: true`` and its "You do not merge
PRs. You do not push commits." rule were documentation, not controls.
"""

from __future__ import annotations

import pytest

from src.models import AgentProfile
from src.sessions.harness_parser import Harness
from src.sessions.spec import SessionSpecBuilder


@pytest.fixture
def resolve():
    builder = SessionSpecBuilder.__new__(SessionSpecBuilder)
    return builder._resolve_allowed_tools


@pytest.fixture
def claude():
    return Harness(id="claude", command="claude", tools_flag="--allowedTools")


def _profile(tools=None, pid="p"):
    return AgentProfile(id=pid, name=pid, allowed_tools=tools or [])


class TestAllowlistResolution:
    def test_harness_tools_are_kept(self, resolve, claude):
        assert resolve(_profile(["Bash", "Read", "Glob", "Grep"]), claude) == [
            "Bash", "Read", "Glob", "Grep",
        ]

    def test_aq_commands_are_dropped(self, resolve, claude):
        """Sessions launch without --mcp-config, so aq commands are not tools.

        They reach the daemon through the ``aq`` CLI (i.e. through Bash), so
        they cannot be named in a harness allowlist.
        """
        got = resolve(_profile(["Bash", "Read", "get_task", "pr_merge"]), claude)
        assert got == ["Bash", "Read"]

    def test_declaration_order_is_preserved(self, resolve, claude):
        got = resolve(_profile(["Grep", "Bash", "Read"]), claude)
        assert got == ["Grep", "Bash", "Read"]


class TestNoFlagEmitted:
    """Empty result means "emit no flag" — the CLI keeps its own defaults."""

    def test_unset_allowlist(self, resolve, claude):
        assert resolve(_profile([]), claude) == []

    def test_wildcard_means_everything(self, resolve, claude):
        assert resolve(_profile(["*"]), claude) == []

    def test_harness_without_a_tools_flag_cannot_restrict(self, resolve):
        codex = Harness(id="codex", command="codex")
        assert resolve(_profile(["Bash", "Read"]), codex) == []

    def test_only_aq_commands_does_not_disable_every_tool(self, resolve, claude):
        """Emitting an empty allowlist would remove Bash — the agent's only
        route to ``aq`` — and strand it. Fall back to CLI defaults instead."""
        assert resolve(_profile(["task_close", "pr_merge"]), claude) == []


class TestHarnessParsesEveryKnownFlag:
    """A key in HARNESS_KNOWN_KEYS must actually reach the Harness object.

    ``tools_flag`` was added to the known-keys set and to the dataclass but not
    to the constructor call, so it parsed without warning and silently
    defaulted to "". The daemon logged "harness 'claude' has no tools_flag" on
    every launch while the markdown plainly declared one — recognised, then
    dropped. This test walks the known keys rather than checking one field, so
    the next flag added cannot repeat it.
    """

    def test_declared_flags_reach_the_dataclass(self):
        import dataclasses

        from src.sessions.harness_parser import (
            HARNESS_KNOWN_KEYS,
            Harness,
            parse_harness_markdown,
        )

        flag_keys = sorted(k for k in HARNESS_KNOWN_KEYS if k.endswith("_flag"))
        assert flag_keys, "expected some *_flag keys to exist"

        fields = {f.name for f in dataclasses.fields(Harness)}
        config = {"command": "x"} | {k: f"--{k}" for k in flag_keys}
        md = (
            "---\nid: probe\nname: Probe\n---\n\n## Config\n\n```json\n"
            + __import__("json").dumps(config)
            + "\n```\n"
        )
        parsed = parse_harness_markdown(md, fallback_id="probe")
        assert parsed.errors == [], parsed.errors

        for key in flag_keys:
            assert key in fields, f"{key} is a known key but not a Harness field"
            assert getattr(parsed.harness, key) == f"--{key}", (
                f"{key} is declared in the markdown and is a Harness field, but "
                "the parser never assigns it — it will silently default"
            )

    def test_claude_harness_declares_a_tools_flag(self):
        """The shipped harness must be able to carry an allowlist at all."""
        import pathlib

        from src.sessions.harness_parser import parse_harness_markdown

        md = pathlib.Path("src/sessions/default_harnesses/claude.md").read_text()
        parsed = parse_harness_markdown(md, fallback_id="claude")
        assert parsed.harness.tools_flag == "--allowedTools"
