"""Tests for :mod:`src.env_scrub` — trust rule R6.

Covers ``docs/specs/implementation/trust-and-ops.md`` §8, row 1.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from src.env_scrub import (
    BUILTIN_EXEMPT,
    HARNESS_CREDENTIAL_ALLOWLIST,
    SENSITIVE_ENV_PATTERNS,
    STRIP_ALWAYS,
    ScrubResult,
    is_sensitive,
    harness_session_markers,
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
            # B2: named in design §3 / the module docstring but previously kept.
            "PG_DSN",
            "SENTRY_DSN",
            "SLACK_WEBHOOK_URL",
            "APIKEY",
            "API-KEY",
            "GH_PAT",
            "GITHUB_PAT",
            "SSH_KEY",
            "ID_RSA",
            "SIGNING_KEY",
            "ENCRYPTION_KEY",
            "SESSION_KEY",
            "PASSPHRASE",
            "NETRC",
            "KUBECONFIG",
        ],
    )
    def test_sensitive_keys_dropped_case_insensitively(self, key):
        # harness_credentials=False so the shipped provider allowlist doesn't
        # rescue vendor-shaped names; that layer is tested separately.
        result = scrub_env({key: "s3cret", "PATH": "/usr/bin"}, harness_credentials=False)
        assert key not in result.env
        assert key in result.dropped
        assert result.env["PATH"] == "/usr/bin"

    @pytest.mark.parametrize(
        "key",
        [
            # Substring lists over-match easily; these must survive.
            "PATH",
            "LD_LIBRARY_PATH",
            "PYTHONPATH",
            "KEYBOARD_LAYOUT",
            "MONKEY_PATCH",
            "GIT_AUTHOR_NAME",
        ],
    )
    def test_near_miss_keys_are_not_dropped(self, key):
        result = scrub_env({key: "v"}, harness_credentials=False)
        assert result.env[key] == "v", f"{key} was dropped as a false positive"

    def test_credentialed_dsn_value_is_dropped_even_with_an_innocent_name(self):
        """Design §3 names database DSNs explicitly; the name alone misses them."""
        result = scrub_env(
            {
                "DATABASE_URL": "postgres://user:password@host/db",
                "REDIS_URL": "redis://localhost:6379/0",
            },
            harness_credentials=False,
        )
        assert "DATABASE_URL" not in result.env
        assert result.dropped == ["DATABASE_URL"]
        # No credentials in the URL, nothing to withhold.
        assert result.env["REDIS_URL"] == "redis://localhost:6379/0"

    def test_dsn_detection_never_leaks_the_value(self):
        result = scrub_env(
            {"SOME_URL": "amqp://admin:hunter2@rabbit/vhost"}, harness_credentials=False
        )
        assert "SOME_URL" in result.dropped
        assert "hunter2" not in " ".join(result.dropped)

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

    def test_harness_session_markers_are_removed_even_when_scrubbing_is_disabled(self):
        source = {
            "CLAUDE_CODE_SESSION_ID": "claude-session",
            "CLAUDE_PID": "123",
            "CLAUDE_EFFORT": "high",
            "ANTHROPIC_AUTH_TOKEN": "session-token",
            "CODEX_SANDBOX": "seatbelt",
            "CODEX_CI": "1",
            "ANTHROPIC_API_KEY": "api-key",
            "CODEX_API_KEY": "api-key",
        }
        result = scrub_env(source, enabled=False)
        assert harness_session_markers(source) == sorted(set(source) - {
            "ANTHROPIC_API_KEY", "CODEX_API_KEY"
        })
        for key in harness_session_markers(source):
            assert key not in result.env
        assert result.env["ANTHROPIC_API_KEY"] == "api-key"
        assert result.env["CODEX_API_KEY"] == "api-key"


class TestAllowlist:
    # Names deliberately outside HARNESS_CREDENTIAL_ALLOWLIST so these tests
    # exercise the operator allowlist rather than the shipped defaults.
    def test_exact_name(self):
        result = scrub_env({"VOYAGE_API_KEY": "sk-1"}, allowlist=["VOYAGE_API_KEY"])
        assert result.env["VOYAGE_API_KEY"] == "sk-1"
        assert result.dropped == []

    def test_case_insensitive_exact_name(self):
        result = scrub_env({"VOYAGE_API_KEY": "sk-1"}, allowlist=["voyage_api_key"])
        assert result.env["VOYAGE_API_KEY"] == "sk-1"

    def test_glob(self):
        result = scrub_env(
            {"COHERE_API_KEY": "a", "VOYAGE_API_KEY": "b", "DISCORD_TOKEN": "c"},
            allowlist=["*_API_KEY"],
        )
        assert result.env["COHERE_API_KEY"] == "a"
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

    def test_explicit_reinstates_a_strip_always_key_with_the_same_value(self):
        """Documented, deliberate: STRIP_ALWAYS beats *inheritance*, not intent.

        ``STRIP_ALWAYS`` exists so an inherited ``CLAUDECODE`` doesn't make a
        nested CLI think it is already in a session.  A harness/profile ``env``
        map that names the key is an operator saying otherwise, and explicit
        intent outranks a value we only inherited.  The module docstring says
        so; this pins the behaviour against a "removed regardless" reading.
        """
        result = scrub_env({"CLAUDECODE": "1"}, explicit={"CLAUDECODE": "1"})
        assert result.env["CLAUDECODE"] == "1"
        assert result.dropped == []

    def test_explicit_adds_new_keys(self):
        result = scrub_env({}, explicit={"AQ_SESSION_ID": "s-1", "AQ_API_TOKEN": "t-1"})
        assert result.env["AQ_SESSION_ID"] == "s-1"
        # An explicitly injected token survives even though it matches TOKEN.
        assert result.env["AQ_API_TOKEN"] == "t-1"


class TestHarnessCredentialAllowlist:
    """The scrub ships default-on; an agent CLI must still be able to log in.

    Design decision recorded in ``docs/specs/design/trust-and-ops.md`` §3: a
    fresh install authenticating by API key rather than ``claude login`` must
    keep working after this lane merges.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "OPENROUTER_API_KEY",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        ],
    )
    def test_harness_credentials_survive_by_default(self, key):
        result = scrub_env({key: "cred"})
        assert result.env[key] == "cred", (
            f"{key} was withheld — an agent harness authenticating with it "
            "cannot start, which breaks a default install"
        )
        assert key not in result.dropped

    @pytest.mark.parametrize(
        "key", ["DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "PG_DSN", "VOYAGE_API_KEY"]
    )
    def test_daemon_secrets_are_still_withheld(self, key):
        """The allowlist is vendor-scoped, not a blanket amnesty."""
        result = scrub_env({key: "x"})
        assert key not in result.env
        assert key in result.dropped

    def test_defaults_can_be_turned_off(self):
        result = scrub_env({"ANTHROPIC_API_KEY": "k"}, harness_credentials=False)
        assert "ANTHROPIC_API_KEY" not in result.env

    def test_every_default_entry_is_actually_a_sensitive_name(self):
        """A default-allowlist entry that nothing would drop is dead weight."""
        for pattern in HARNESS_CREDENTIAL_ALLOWLIST:
            sample = pattern.replace("*", "API_KEY")
            assert is_sensitive(sample), (
                f"{pattern!r} allows {sample!r}, which the scrub would keep anyway"
            )


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


class TestSessionEnvHonoursTheConfig:
    """The **real** call site: ``build_session_env`` → ``scrub_env_from_config``.

    Regression pin for the defect this replaces: the config-aware scrub was
    once only ever reached from a test.  Production called the config-less
    variant, so ``security.env_scrub_enabled`` and ``security.env_allowlist``
    were unreachable — a green test over a non-functional feature.

    The call site moved when the tmux-harness migration deleted the ACPX
    runtime and its ``isolated_env`` helper.  Every coding agent now launches
    through ``build_session_env``, so that is what these assertions go
    through.  The pin matters more than where it points.
    """

    @staticmethod
    def _build(config=None, base=None, harness_env=None):
        from src.sessions.env import build_session_env

        return build_session_env(
            session_id="s-1",
            task_id="t-1",
            project_id="p-1",
            profile_id="prof-1",
            epoch="2026-08-24T00:00:00Z",
            instance_token="tok",
            work_dir="/tmp/ws",
            api_url="http://127.0.0.1:8081",
            api_token="api-tok",
            harness_env=harness_env,
            config=config,
            base=base,
        )

    def test_daemon_secrets_are_withheld_from_the_agent(self):
        env = self._build(base={"DISCORD_BOT_TOKEN": "secret", "PATH": "/usr/bin"})
        assert "DISCORD_BOT_TOKEN" not in env
        assert env["PATH"] == "/usr/bin"

    def test_agent_keeps_its_provider_credentials(self):
        env = self._build(base={"ANTHROPIC_API_KEY": "k", "PATH": "/usr/bin"})
        assert env["ANTHROPIC_API_KEY"] == "k"

    def test_kill_switch_reaches_the_real_call_site(self):
        from src.config import AppConfig

        cfg = AppConfig()
        cfg.security.env_scrub_enabled = False
        env = self._build(config=cfg, base={"DISCORD_BOT_TOKEN": "secret"})
        assert env["DISCORD_BOT_TOKEN"] == "secret", (
            "security.env_scrub_enabled=False must disable scrubbing at the "
            "real launch site, not only in a unit test"
        )

    def test_operator_allowlist_reaches_the_real_call_site(self):
        from src.config import AppConfig

        cfg = AppConfig()
        cfg.security.env_allowlist = ["DISCORD_BOT_TOKEN"]
        env = self._build(config=cfg, base={"DISCORD_BOT_TOKEN": "secret"})
        assert env["DISCORD_BOT_TOKEN"] == "secret"

    def test_harness_env_is_explicit_and_survives_the_scrub(self):
        """A key named in a harness file is meant, so it outranks the scrub."""
        env = self._build(
            base={"PATH": "/usr/bin"},
            harness_env={"DISCORD_BOT_TOKEN": "named-on-purpose"},
        )
        assert env["DISCORD_BOT_TOKEN"] == "named-on-purpose"


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

    async def test_provider_credentials_are_withheld_from_the_daemon_shell(
        self, tmp_path, monkeypatch
    ):
        """A diagnostic shell is not an agent harness — no vendor keys.

        The harness allowlist exists so an agent CLI can authenticate.  The
        LLM-authored shell in ``run_command`` has no such need, so it opts out
        (``harness_credentials=False``).
        """
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
        handler = Handler(config)

        with patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "sk-ant", "PATH": "/usr/bin"}, clear=True
        ):
            await handler._cmd_run_command(
                {"command": "echo hi", "working_dir": str(tmp_path)}
            )

        assert "ANTHROPIC_API_KEY" not in captured["env"]
        assert captured["env"]["PATH"] == "/usr/bin"
