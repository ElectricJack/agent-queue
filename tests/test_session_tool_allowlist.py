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
            "Bash", "Glob", "Grep", "Read",
        ]

    def test_aq_commands_live_in_their_own_namespace(self, resolve, claude):
        """AQ command names never reach the harness allowlist flag.

        Sessions launch without --mcp-config, so an aq command is not a tool
        the CLI can be told about — it reaches the daemon through the ``aq``
        CLI (i.e. through Bash).  Package 0 makes that structural rather than
        a filtering step: ``aq_commands`` is a separate namespace, gated
        server-side at dispatch (``src/commands/authorization.py``).
        """
        got = resolve(_profile(["Bash", "Read", "get_task", "pr_merge"]), claude)
        assert got == ["Bash", "Read"]

    def test_output_is_deterministic(self, resolve, claude):
        """Sorted, not declaration-ordered.

        ``harness_tools`` is a set, so declaration order carries no meaning
        and cannot be preserved.  Sorting keeps the emitted argv stable
        across runs, which matters because the argv is what a restarted
        session is compared against.
        """
        assert resolve(_profile(["Grep", "Bash", "Read"]), claude) == ["Bash", "Grep", "Read"]
        assert resolve(_profile(["Read", "Grep", "Bash"]), claude) == ["Bash", "Grep", "Read"]

    def test_explicit_capabilities_win_over_allowed_tools(self, resolve, claude):
        profile = AgentProfile(
            id="p", name="p",
            allowed_tools=["Bash", "Read", "Write", "Edit"],
            harness_tools=["Bash", "Read"],
            aq_commands=["task_close"],
            plugin_tools=[],
        )
        assert resolve(profile, claude) == ["Bash", "Read"]

    def test_explicitly_empty_harness_tools_emits_no_flag(self, resolve, claude):
        profile = AgentProfile(
            id="p", name="p", harness_tools=[], aq_commands=[], plugin_tools=[]
        )
        assert resolve(profile, claude) == []


class TestNoFlagEmitted:
    """Empty result means "emit no flag" — the CLI keeps its own defaults."""

    def test_unset_allowlist_falls_back_to_the_names_the_launcher_knows(
        self, resolve, claude
    ):
        """Legacy adapter rule R1 (Playbook V2 Package 0 §3.3).

        An empty ``allowed_tools`` used to mean "emit no flag", i.e. the
        CLI's own defaults.  The adapter now yields the 12 names the launcher
        recognises — the same effective grant, because the flag could never
        have expressed more than those names anyway.  It grants nothing new.
        """
        from src.profiles.capabilities import HARNESS_TOOL_NAMES

        assert resolve(_profile([]), claude) == sorted(HARNESS_TOOL_NAMES)

    def test_wildcard_cannot_reach_this_function(self, resolve, claude):
        """``"*"`` is rejected at parse and at sync, so it never gets here.

        It used to mean "everything" — the grant-everything value Package 0
        exists to remove.  Reaching the launcher with one now raises rather
        than silently widening.
        """
        from src.profiles.capabilities import CapabilityPolicyError

        with pytest.raises(CapabilityPolicyError, match="wildcard"):
            resolve(_profile(["*"]), claude)

    def test_wildcard_is_rejected_at_parse_time(self):
        from src.profiles.parser import parse_profile

        parsed = parse_profile(
            '---\nid: p\nname: P\n---\n\n## Tools\n\n```json\n'
            '{"allowed": ["*"]}\n```\n'
        )
        assert any("wildcard" in e for e in parsed.errors), parsed.errors

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
