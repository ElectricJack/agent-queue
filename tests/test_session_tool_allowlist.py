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
