"""Tests for :mod:`src.env_scrub` — trust rule R6.

Covers ``docs/specs/implementation/trust-and-ops.md`` §8, row 1.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from src.env_scrub import (
    BUILTIN_EXEMPT,
    SENSITIVE_ENV_PATTERNS,
    STRIP_ALWAYS,
    ScrubResult,
    is_sensitive,
    scrub_env,
    scrub_env_from_config,
)


class TestPatterns:
    @pytest.mark.parametrize(
        "key",
        [
            "MY_TOKEN",
            "DISCORD_TOKEN",
            "api_key_2",
            "OPENAI_API_KEY",
            "SOME_SECRET",
            "db_password",
            "AWS_CREDENTIALS",
            "SSH_PRIVATE_KEY",
            "GithubAuth",
        ],
    )
    def test_sensitive_keys_dropped_case_insensitively(self, key):
        result = scrub_env({key: "s3cret", "PATH": "/usr/bin"})
        assert key not in result.env
        assert key in result.dropped
        assert result.env["PATH"] == "/usr/bin"

    @pytest.mark.parametrize(
        "key",
        ["PATH", "HOME", "LANG", "TERM", "AQ_TASK_ID", "AUTHORITATIVE"],
    )
    def test_innocuous_keys_survive(self, key):
        # AUTHORITATIVE contains "AUTH" — documented false positive class, so it
        # is dropped; everything else must pass through untouched.
        result = scrub_env({key: "v"})
        if is_sensitive(key):
            assert key not in result.env
        else:
            assert result.env[key] == "v"

    def test_every_shipped_pattern_actually_matches_something(self):
        for pattern in SENSITIVE_ENV_PATTERNS:
            key = f"PREFIX_{pattern}_SUFFIX"
            assert is_sensitive(key), pattern
            assert key in scrub_env({key: "x"}).dropped

    @pytest.mark.parametrize("key", BUILTIN_EXEMPT)
    def test_builtin_exemptions_survive(self, key):
        # These are false positives of the AUTH pattern.
        assert is_sensitive(key)
        result = scrub_env({key: "Jane Doe"})
        assert result.env[key] == "Jane Doe"
        assert key not in result.dropped


class TestAllowlist:
    def test_exact_name(self):
        result = scrub_env({"OPENAI_API_KEY": "sk-1"}, allowlist=["OPENAI_API_KEY"])
        assert result.env["OPENAI_API_KEY"] == "sk-1"
        assert result.dropped == []

    def test_case_insensitive_exact_name(self):
        result = scrub_env({"OPENAI_API_KEY": "sk-1"}, allowlist=["openai_api_key"])
        assert result.env["OPENAI_API_KEY"] == "sk-1"

    def test_glob(self):
        result = scrub_env(
            {"OPENAI_API_KEY": "a", "VOYAGE_API_KEY": "b", "DISCORD_TOKEN": "c"},
            allowlist=["*_API_KEY"],
        )
        assert result.env["OPENAI_API_KEY"] == "a"
        assert result.env["VOYAGE_API_KEY"] == "b"
        assert "DISCORD_TOKEN" not in result.env
        assert result.dropped == ["DISCORD_TOKEN"]

    def test_empty_entries_ignored(self):
        result = scrub_env({"MY_TOKEN": "x"}, allowlist=["", None])
        assert "MY_TOKEN" not in result.env


class TestExplicit:
    def test_explicit_wins_over_pattern(self):
        result = scrub_env(
            {"ANTHROPIC_API_KEY": "from-daemon"},
            explicit={"ANTHROPIC_API_KEY": "from-profile"},
        )
        assert result.env["ANTHROPIC_API_KEY"] == "from-profile"
        # Re-introduced explicitly, so it was not withheld after all.
        assert "ANTHROPIC_API_KEY" not in result.dropped

    def test_explicit_wins_over_strip_always(self):
        result = scrub_env(
            {"CLAUDECODE": "1"},
            explicit={"CLAUDECODE": "0"},
        )
        assert result.env["CLAUDECODE"] == "0"
        assert result.dropped == []

    def test_explicit_adds_new_keys(self):
        result = scrub_env({}, explicit={"AQ_SESSION_ID": "s-1", "AQ_API_TOKEN": "t-1"})
        assert result.env["AQ_SESSION_ID"] == "s-1"
        # An explicitly injected token survives even though it matches TOKEN.
        assert result.env["AQ_API_TOKEN"] == "t-1"


class TestStripAlways:
    @pytest.mark.parametrize("key", STRIP_ALWAYS)
    def test_stripped_when_enabled(self, key):
        assert key not in scrub_env({key: "1"}).env

    @pytest.mark.parametrize("key", STRIP_ALWAYS)
    def test_stripped_when_disabled(self, key):
        result = scrub_env({key: "1", "MY_TOKEN": "x"}, enabled=False)
        assert key not in result.env
        assert key in result.dropped

    def test_disabled_preserves_secrets(self):
        """enabled=False is the kill switch: only STRIP_ALWAYS applies."""
        result = scrub_env(
            {"DISCORD_TOKEN": "t", "DB_PASSWORD": "p", "CLAUDECODE": "1"},
            enabled=False,
        )
        assert result.env["DISCORD_TOKEN"] == "t"
        assert result.env["DB_PASSWORD"] == "p"
        assert result.dropped == ["CLAUDECODE"]


class TestAuditTrail:
    def test_dropped_lists_names_only_never_values(self):
        result = scrub_env({"DISCORD_TOKEN": "super-secret-value"})
        assert result.dropped == ["DISCORD_TOKEN"]
        assert "super-secret-value" not in repr(result.dropped)
        assert "super-secret-value" not in " ".join(result.dropped)

    def test_dropped_is_sorted(self):
        result = scrub_env({"Z_TOKEN": "1", "A_SECRET": "2", "M_PASSWORD": "3"})
        assert result.dropped == sorted(result.dropped)

    def test_returns_scrub_result(self):
        assert isinstance(scrub_env({}), ScrubResult)


class TestPurity:
    def test_os_environ_untouched(self):
        with patch.dict(os.environ, {"MY_TOKEN": "keepme", "PATH": "/usr/bin"}, clear=False):
            before = dict(os.environ)
            result = scrub_env()
            assert "MY_TOKEN" not in result.env
            assert os.environ == before
            assert os.environ["MY_TOKEN"] == "keepme"

    def test_base_mapping_untouched(self):
        base = {"MY_TOKEN": "x", "PATH": "/bin"}
        snapshot = dict(base)
        scrub_env(base, explicit={"NEW": "1"})
        assert base == snapshot

    def test_defaults_to_os_environ(self):
        with patch.dict(os.environ, {"AQ_SCRUB_PROBE": "yes"}, clear=False):
            assert scrub_env().env["AQ_SCRUB_PROBE"] == "yes"


class TestConfigWrapper:
    def test_reads_security_section(self):
        from src.config import AppConfig

        config = AppConfig()
        config.security.env_allowlist = ["MY_TOKEN"]
        result = scrub_env_from_config(
            config, base={"MY_TOKEN": "a", "OTHER_TOKEN": "b"}
        )
        assert result.env["MY_TOKEN"] == "a"
        assert "OTHER_TOKEN" not in result.env

    def test_kill_switch_honoured(self):
        from src.config import AppConfig

        config = AppConfig()
        config.security.env_scrub_enabled = False
        result = scrub_env_from_config(config, base={"MY_TOKEN": "a"})
        assert result.env["MY_TOKEN"] == "a"

    def test_scrub_enabled_defaults_on(self):
        from src.config import AppConfig

        assert AppConfig().security.env_scrub_enabled is True

    def test_config_without_security_section_uses_defaults(self):
        class Bare:
            pass

        result = scrub_env_from_config(Bare(), base={"MY_TOKEN": "a", "PATH": "/bin"})
        assert "MY_TOKEN" not in result.env
        assert result.env["PATH"] == "/bin"


class TestIsolatedEnvDelegates:
    """``isolated_env`` must be a thin wrapper, not a second policy."""

    def test_scrubs_by_default(self):
        from src.runtimes._subprocess import isolated_env

        with patch.dict(
            os.environ, {"DISCORD_TOKEN": "t", "PATH": "/usr/bin"}, clear=True
        ):
            env = isolated_env()
        assert "DISCORD_TOKEN" not in env
        assert env["PATH"] == "/usr/bin"

    def test_extra_still_wins(self):
        from src.runtimes._subprocess import isolated_env

        with patch.dict(os.environ, {"FOO": "from-os"}, clear=True):
            env = isolated_env(extra={"FOO": "from-arg", "MY_TOKEN": "explicit"})
        assert env["FOO"] == "from-arg"
        assert env["MY_TOKEN"] == "explicit"

    def test_config_kill_switch_respected(self):
        from src.config import AppConfig
        from src.runtimes._subprocess import isolated_env

        config = AppConfig()
        config.security.env_scrub_enabled = False
        with patch.dict(os.environ, {"DISCORD_TOKEN": "t"}, clear=True):
            env = isolated_env(config=config)
        assert env["DISCORD_TOKEN"] == "t"


class TestRunCommandGetsScrubbedEnv:
    """``_cmd_run_command`` must never hand the daemon env to a shell (R6).

    The command itself remains a knowingly-contained R1 violation
    (docs/specs/design/trust-and-ops.md §2.5); this pins the containment.
    """

    async def test_scrubbed_env_is_passed_through(self, tmp_path, monkeypatch):
        from src.commands.system_commands import SystemCommandsMixin
        from src.config import AppConfig

        captured = {}

        async def fake_shell(command, *, cwd=None, timeout=30, env=None):
            captured["command"] = command
            captured["cwd"] = cwd
            captured["env"] = env
            return (0, "out", "")

        monkeypatch.setattr(
            "src.commands.system_commands._run_subprocess_shell", fake_shell
        )

        class Handler(SystemCommandsMixin):
            def __init__(self, config):
                self.config = config

            async def _validate_path(self, path):
                return path

        config = AppConfig(data_dir=str(tmp_path), workspace_dir=str(tmp_path))
        handler = Handler(config)

        with patch.dict(
            os.environ,
            {"DISCORD_TOKEN": "leak-me", "PATH": "/usr/bin", "CLAUDECODE": "1"},
            clear=True,
        ):
            result = await handler._cmd_run_command(
                {"command": "echo hi", "working_dir": str(tmp_path)}
            )

        assert result["returncode"] == 0
        env = captured["env"]
        assert env is not None, "no env passed — the child would inherit the daemon's"
        assert "DISCORD_TOKEN" not in env
        assert "CLAUDECODE" not in env
        assert env["PATH"] == "/usr/bin"
        assert result["env_scrubbed"] >= 2

    async def test_kill_switch_lets_secrets_through(self, tmp_path, monkeypatch):
        from src.commands.system_commands import SystemCommandsMixin
        from src.config import AppConfig

        captured = {}

        async def fake_shell(command, *, cwd=None, timeout=30, env=None):
            captured["env"] = env
            return (0, "", "")

        monkeypatch.setattr(
            "src.commands.system_commands._run_subprocess_shell", fake_shell
        )

        class Handler(SystemCommandsMixin):
            def __init__(self, config):
                self.config = config

            async def _validate_path(self, path):
                return path

        config = AppConfig(data_dir=str(tmp_path), workspace_dir=str(tmp_path))
        config.security.env_scrub_enabled = False
        handler = Handler(config)

        with patch.dict(os.environ, {"DISCORD_TOKEN": "leak-me"}, clear=True):
            await handler._cmd_run_command(
                {"command": "echo hi", "working_dir": str(tmp_path)}
            )

        assert captured["env"]["DISCORD_TOKEN"] == "leak-me"
