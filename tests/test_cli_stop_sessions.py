"""``aq stop`` ends the daemon's agent sessions; ``aq restart`` keeps them.

Agent tmux sessions outlive the daemon on purpose — ``sessions.adopt_on_start``
re-adopts them so a restart does not discard in-flight work. That is right for
a restart and wrong for a stop: "shut down agent-queue" that leaves agents
running against a dead API, unable to reach ``aq`` to report anything, is not
a shutdown.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from src.cli.daemon import _AQ_SESSION_PREFIXES, stop_agent_sessions


def _run(returncode=0, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestStopAgentSessions:
    def test_kills_only_daemon_owned_sessions(self):
        """Other tmux sessions on the socket belong to someone else."""
        listing = "s-task-one\nn-supervisor--global\nmy-own-shell\ns-task-two\n"
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if "list-sessions" in cmd:
                return _run(stdout=listing)
            return _run()

        with patch("subprocess.run", side_effect=fake_run):
            stopped = stop_agent_sessions(quiet=True)

        assert stopped == 3
        killed = [c[-1] for c in calls if "kill-session" in c]
        assert sorted(killed) == ["n-supervisor--global", "s-task-one", "s-task-two"]
        assert "my-own-shell" not in killed

    def test_no_tmux_server_is_not_an_error(self):
        """A stopped daemon with no sessions must not fail the command."""
        with patch("subprocess.run", return_value=_run(returncode=1)):
            assert stop_agent_sessions(quiet=True) == 0

    def test_tmux_missing_is_survivable(self):
        with patch("subprocess.run", side_effect=OSError("no tmux")):
            assert stop_agent_sessions(quiet=True) == 0

    def test_one_failed_kill_does_not_abort_the_rest(self):
        listing = "s-a\ns-b\n"

        def fake_run(cmd, **kw):
            if "list-sessions" in cmd:
                return _run(stdout=listing)
            return _run(returncode=1) if cmd[-1] == "s-a" else _run()

        with patch("subprocess.run", side_effect=fake_run):
            assert stop_agent_sessions(quiet=True) == 1

    def test_prefixes_match_what_the_reconciler_adopts(self):
        """Reaping must cover exactly the names adoption scans for."""
        assert set(_AQ_SESSION_PREFIXES) == {"s-", "n-"}
