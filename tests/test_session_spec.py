"""SessionSpecBuilder — names, argv composition, prompt delivery, env.

See docs/specs/implementation/session-runtime.md §3.4 and §8.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from src.intelligence_classes import IntelligenceClass
from src.sessions.env import AQ_MARKER_KEYS, STARTUP_PROMPT_DELIVERED, build_session_env
from src.sessions.harness_parser import Harness, ResumeSpec
from src.sessions.spec import (
    SessionSpecBuilder,
    named_session_name,
    sanitize_name,
    task_session_name,
)


@dataclass
class _Task:
    id: str = "task-1"
    project_id: str = "proj-1"


@dataclass
class _Profile:
    id: str = "claude-opus"
    default_class: str = ""
    effort: str = ""
    harness: str = "claude"
    permission_mode: str = ""
    codex_full_auto: bool = False
    claude_dangerously_skip_permissions: bool = False


class _McpCfg:
    host = "127.0.0.1"
    port = 8081


class _Cfg:
    mcp_server = _McpCfg()
    security = None
    data_dir = "/tmp/aq"


CLAUDE = Harness(
    id="claude",
    name="Claude Code",
    command="claude",
    prompt_mode="arg",
    permission_flag="--dangerously-skip-permissions",
    model_flag="--model",
    session_id_flag="--session-id",
    resume=ResumeSpec(style="flag", flag="--resume"),
    process_names=("claude", "node"),
    max_argv_prompt_bytes=1024,
)


@pytest.fixture
def builder():
    return SessionSpecBuilder(_Cfg())


def _classy_builder():
    """A builder whose ``deep`` intelligence class resolves to ``opus``."""
    return SessionSpecBuilder(
        _Cfg(),
        intelligence_classes={
            "deep": IntelligenceClass(
                id="deep",
                name="Deep",
                description="",
                mapping={"anthropic": {"model": "opus"}},
            )
        },
    )


def _build(builder, harness=CLAUDE, profile=None, **kw):
    return builder.build_task_spec(
        task=_Task(),
        profile=profile or _Profile(),
        harness=harness,
        work_dir="/wd",
        session_id="sess-abc",
        instance_token="tok-1",
        epoch="epoch-1",
        api_url="http://127.0.0.1:8081",
        api_token="token-xyz",
        **kw,
    )


def _isolated(builder, **kw):
    """A build in a real per-task worktree — where skip-permissions applies."""
    from src.models import RepoSourceType

    return _build(builder, workspace_source_type=RepoSourceType.WORKTREE, **kw)


class TestNames:
    def test_task_name(self):
        assert task_session_name("task-1") == "s-task-1"

    def test_named_without_project(self):
        assert named_session_name("supervisor") == "n-supervisor"

    def test_named_with_project(self):
        assert named_session_name("supervisor", "proj-1") == "n-supervisor--proj-1"

    def test_sanitization_folds_unsafe_characters(self):
        assert sanitize_name("project:web/dev thing") == "project-web-dev-thing"
        assert sanitize_name("a..b") == "a-b"

    def test_sanitized_names_match_the_declared_charset(self):
        import re

        for raw in ("project:web dev", "task/42", "-leading-", "ünïcødé"):
            assert re.fullmatch(r"[A-Za-z0-9_-]+", sanitize_name(raw))

    def test_empty_input_never_produces_an_empty_name(self):
        assert sanitize_name("///") == "unnamed"

    def test_scoped_profile_id_survives_into_a_usable_name(self):
        assert named_session_name("project:web:reviewer", "proj-1") == (
            "n-project-web-reviewer--proj-1"
        )


class TestSkipPermissionsGating:
    """B5 — trust-and-ops §4 scopes the flag to isolated worktrees.

    §4: skip-permissions applies *"when — and only when — the session's
    ``work_dir`` is an isolated per-task worktree"*, and *"sessions outside
    an isolated worktree do not get skip-permissions by default; profiles
    must opt in."*  ``_compose_argv`` used to append the flag whenever the
    harness declared one, so on today's ``LINK`` workspaces it ran
    ``--dangerously-skip-permissions`` in the operator's real checkout.
    """

    FLAG = "--dangerously-skip-permissions"

    def test_a_worktree_gets_the_flag(self, builder):
        assert self.FLAG in _isolated(builder).command

    def test_a_linked_checkout_does_not(self, builder):
        from src.models import RepoSourceType

        spec = _build(builder, workspace_source_type=RepoSourceType.LINK)
        assert self.FLAG not in spec.command

    def test_a_clone_does_not(self, builder):
        from src.models import RepoSourceType

        spec = _build(builder, workspace_source_type=RepoSourceType.CLONE)
        assert self.FLAG not in spec.command

    def test_an_unknown_workspace_defaults_to_withholding(self, builder):
        """The restrictive default: no source type means no flag."""
        assert self.FLAG not in _build(builder).command

    def test_a_profile_can_opt_in_explicitly(self, builder):
        from src.models import RepoSourceType

        spec = _build(
            builder,
            profile=_Profile(permission_mode="bypassPermissions"),
            workspace_source_type=RepoSourceType.LINK,
        )
        assert self.FLAG in spec.command

    def test_claude_profile_boolean_can_opt_in_explicitly(self, builder):
        from src.models import RepoSourceType

        spec = _build(
            builder,
            profile=_Profile(claude_dangerously_skip_permissions=True),
            workspace_source_type=RepoSourceType.LINK,
        )
        assert spec.command.count(self.FLAG) == 1

    def test_claude_profile_boolean_does_not_affect_another_harness(self, builder):
        codex = Harness(
            id="codex",
            command="codex",
            permission_flag="--dangerously-bypass-approvals-and-sandbox",
        )
        spec = _build(
            builder,
            harness=codex,
            profile=_Profile(claude_dangerously_skip_permissions=True),
        )
        assert self.FLAG not in spec.command
        assert "--dangerously-bypass-approvals-and-sandbox" not in spec.command

    def test_claude_derived_flag_is_not_duplicated_from_harness_args(self, builder):
        harness = replace(CLAUDE, args=(self.FLAG,))
        profile = _Profile(
            permission_mode="bypassPermissions",
            claude_dangerously_skip_permissions=True,
        )
        spec = _build(builder, harness=harness, profile=profile)
        assert spec.command.count(self.FLAG) == 1

    def test_false_claude_profile_boolean_preserves_raw_harness_arg(self, builder):
        harness = replace(CLAUDE, args=(self.FLAG,))
        spec = _build(
            builder,
            harness=harness,
            profile=_Profile(claude_dangerously_skip_permissions=False),
        )
        assert spec.command.count(self.FLAG) == 1

    def test_another_permission_mode_is_not_an_opt_in(self, builder):
        from src.models import RepoSourceType

        spec = _build(
            builder,
            profile=_Profile(permission_mode="acceptEdits"),
            workspace_source_type=RepoSourceType.LINK,
        )
        assert self.FLAG not in spec.command

    def test_a_harness_without_the_flag_never_gets_one(self, builder):
        harness = replace(CLAUDE, permission_flag="")
        assert self.FLAG not in _isolated(builder, harness=harness).command

    def test_a_named_session_profile_can_opt_in(self, builder):
        """Named sessions have no workspace; the profile opt-in is the only
        way for e.g. the supervisor (vault work_dir) to skip prompts."""
        spec = builder.build_named_spec(
            profile=_Profile(id="supervisor", permission_mode="bypassPermissions"),
            harness=CLAUDE,
            project_id="proj-1",
            work_dir="/vault/projects/proj-1",
            session_id="s1",
            instance_token="t1",
        )
        assert self.FLAG in spec.command

    def test_a_named_session_without_opt_in_is_withheld(self, builder):
        spec = builder.build_named_spec(
            profile=_Profile(id="supervisor"),
            harness=CLAUDE,
            project_id="proj-1",
            work_dir="/vault/projects/proj-1",
            session_id="s1",
            instance_token="t1",
        )
        assert self.FLAG not in spec.command

    def test_the_policy_helper_is_the_single_definition(self):
        from src.models import RepoSourceType
        from src.sessions.spec import skip_permissions_allowed

        assert skip_permissions_allowed(_Profile(), RepoSourceType.WORKTREE) is True
        assert skip_permissions_allowed(_Profile(), RepoSourceType.LINK) is False
        assert skip_permissions_allowed(_Profile(), None) is False
        assert (
            skip_permissions_allowed(
                _Profile(permission_mode="bypassPermissions"), None
            )
            is True
        )


class TestHookSettingsWiring:
    """The hook payload has to be *pointed at*, not merely written."""

    def test_settings_flag_is_emitted_when_hooks_render(self, builder, tmp_path):
        harness = replace(
            CLAUDE,
            supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
            settings_flag="--settings",
        )
        spec = _build(builder, harness=harness)
        argv = list(spec.command)
        assert "--settings" in argv
        assert argv[argv.index("--settings") + 1] == ".aq/hooks/claude.json"
        assert any(dest == ".aq/hooks/claude.json" for dest, _ in spec.files)

    def test_no_settings_flag_when_the_harness_writes_no_hooks(self, builder):
        assert "--settings" not in _build(builder).command

    def test_the_shipped_claude_harness_wires_them_together(self, tmp_path):
        """``supports_hooks: true`` must not be an advertisement for a dead file."""
        from src.sessions.harness_registry import HarnessRegistry, load_from_vault
        from src.vault import ensure_default_harnesses

        ensure_default_harnesses(str(tmp_path))
        registry = HarnessRegistry()
        load_from_vault(registry, str(tmp_path / "vault"))
        claude = registry.get("claude", None)
        assert claude.supports_hooks and claude.hook_files
        assert claude.settings_flag, (
            "claude declares hook_files but no settings_flag — the payload is inert"
        )


class TestCodexHookTrust:
    """Codex discovers its hook file by path and refuses to run it untrusted."""

    CODEX = Harness(
        id="codex",
        name="OpenAI Codex",
        command="codex",
        permission_flag="--dangerously-bypass-approvals-and-sandbox",
        supports_hooks=True,
        hook_files=((".codex/hooks.json", "hooks/codex.json"),),
        hook_trust_flag="--dangerously-bypass-hook-trust",
    )

    def test_the_file_is_written_into_the_work_dir_with_no_settings_flag(self, builder):
        spec = _isolated(builder, harness=self.CODEX)
        by_path = dict(spec.files)
        assert ".codex/hooks.json" in by_path
        assert "--settings" not in spec.command

    def test_the_trust_flag_rides_the_isolated_worktree_argument(self, builder):
        assert "--dangerously-bypass-hook-trust" in _isolated(
            builder, harness=self.CODEX
        ).command

    def test_a_linked_checkout_keeps_hook_review_and_loses_the_telemetry(self, builder):
        from src.models import RepoSourceType

        spec = _build(
            builder, harness=self.CODEX, workspace_source_type=RepoSourceType.LINK
        )
        # A repo we do not own may carry its own .codex/hooks.json; we do not
        # pre-trust that just to count sub-agents.
        assert "--dangerously-bypass-hook-trust" not in spec.command
        assert spec.hooks_provisioned is False

    def test_hooks_provisioned_records_whether_the_launch_actually_wired_them(self, builder):
        assert _isolated(builder, harness=self.CODEX).hooks_provisioned is True
        claude = replace(
            CLAUDE, supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
            settings_flag="--settings",
        )
        assert _build(builder, harness=claude).hooks_provisioned is True
        # No hook file at all — nothing to be provisioned by.
        assert _build(builder).hooks_provisioned is False
        # Declares a file but nothing makes it live: the "dead file" case.
        inert = replace(
            CLAUDE, supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
            settings_flag="",
        )
        assert _build(builder, harness=inert).hooks_provisioned is False

    def test_the_codex_payload_wires_only_the_two_subagent_events(self, builder):
        import json as _json

        payload = _json.loads(
            dict(_isolated(builder, harness=self.CODEX).files)[".codex/hooks.json"]
        )
        assert set(payload["hooks"]) == {"SubagentStart", "SubagentStop"}
        entry = payload["hooks"]["SubagentStart"][0]["hooks"][0]
        # Codex refuses to parse a handler without "type" and then silently
        # runs no hooks at all — measured on codex-cli 0.151.0.
        assert entry["type"] == "command"
        assert entry["command"] == "aq subagent event --hook-json"

    def test_the_shipped_codex_harness_declares_the_trust_flag(self, tmp_path):
        """``supports_hooks: true`` with no way to be trusted is a dead file."""
        from src.sessions.harness_registry import HarnessRegistry, load_from_vault
        from src.vault import ensure_default_harnesses

        ensure_default_harnesses(str(tmp_path))
        registry = HarnessRegistry()
        load_from_vault(registry, str(tmp_path / "vault"))
        codex = registry.get("codex", None)
        assert codex.supports_hooks and codex.hook_files
        assert not codex.settings_flag, "codex has no settings-file flag"
        assert codex.hook_trust_flag, (
            "codex declares hook_files but nothing that makes them run"
        )


class TestArgvComposition:
    def test_codex_full_auto_profile_opt_in_adds_flag_once(self, builder):
        codex = Harness(id="codex", command="codex", args=("--quiet",))
        spec = _build(builder, harness=codex, profile=_Profile(codex_full_auto=True))
        assert spec.command.count("--full-auto") == 1
        assert spec.command.index("--quiet") < spec.command.index("--full-auto")

    def test_codex_full_auto_defaults_off(self, builder):
        codex = Harness(id="codex", command="codex")
        spec = _build(builder, harness=codex, profile=_Profile())
        assert "--full-auto" not in spec.command

    def test_codex_full_auto_does_not_affect_another_harness(self, builder):
        spec = _build(builder, profile=_Profile(codex_full_auto=True))
        assert "--full-auto" not in spec.command

    def test_codex_full_auto_is_not_duplicated_from_harness_args(self, builder):
        codex = Harness(id="codex", command="codex", args=("--full-auto",))
        spec = _build(builder, harness=codex, profile=_Profile(codex_full_auto=True))
        assert spec.command.count("--full-auto") == 1

    def test_legacy_codex_bypass_takes_precedence_over_derived_full_auto(self, builder):
        bypass = "--dangerously-bypass-approvals-and-sandbox"
        codex = Harness(
            id="codex",
            command="codex",
            args=("--full-auto",),
            permission_flag=bypass,
        )
        profile = _Profile(
            permission_mode="bypassPermissions",
            codex_full_auto=True,
        )
        spec = _build(builder, harness=codex, profile=profile)
        assert spec.command.count(bypass) == 1
        assert "--full-auto" not in spec.command

    def test_false_codex_profile_boolean_preserves_raw_harness_arg(self, builder):
        codex = Harness(id="codex", command="codex", args=("--full-auto",))
        spec = _build(builder, harness=codex, profile=_Profile(codex_full_auto=False))
        assert spec.command.count("--full-auto") == 1

    def test_basic_argv(self, builder):
        spec = _isolated(builder)
        assert spec.command[0] == "claude"
        assert "--dangerously-skip-permissions" in spec.command
        # Prompt rides argv as the final positional.
        assert spec.command[-1] == spec.prompt

    def test_the_bootstrap_prompt_asks_for_heartbeats(self, builder):
        """H3 — nothing else tells the agent the lease exists.

        With S3 deferred the lease has two feeds, and on a nudgeless
        provider a long quiet tool call goes straight to interrupt+kill.
        """
        spec = _build(builder)
        assert "aq task heartbeat" in spec.prompt

    def test_model_flag_only_when_the_class_resolves_a_model(self, builder):
        """The launch model comes from the profile's intelligence class.

        A profile cannot pin one: the ``model`` Config key was removed, so with
        no class (or a class the registry does not know) there is no --model.
        """
        assert "--model" not in _build(builder).command
        assert "--model" not in _build(builder, profile=_Profile(default_class="deep")).command
        spec = _build(_classy_builder(), profile=_Profile(default_class="deep"))
        argv = list(spec.command)
        assert argv[argv.index("--model") + 1] == "opus"

    def test_model_flag_skipped_when_the_harness_has_none(self):
        harness = replace(CLAUDE, model_flag="")
        spec = _build(
            _classy_builder(), harness=harness, profile=_Profile(default_class="deep")
        )
        assert "opus" not in spec.command

    def test_effort_flag(self, builder):
        harness = replace(CLAUDE, effort_flag="--effort")
        spec = _build(builder, harness=harness, profile=_Profile(effort="high"))
        argv = list(spec.command)
        assert argv[argv.index("--effort") + 1] == "high"

    def test_session_id_flag_pins_our_id_when_not_resuming(self, builder):
        spec = _build(builder)
        argv = list(spec.command)
        assert argv[argv.index("--session-id") + 1] == "sess-abc"

    def test_resume_flag_style_appends_the_key(self, builder):
        spec = _build(builder, resume_key="prev-key")
        argv = list(spec.command)
        assert argv[argv.index("--resume") + 1] == "prev-key"

    def test_session_id_flag_is_not_combined_with_resume(self, builder):
        """A resumed session already has an id; passing both is a conflict."""
        spec = _build(builder, resume_key="prev-key")
        assert "--session-id" not in spec.command

    def test_resume_subcommand_style_goes_before_the_flags(self, builder):
        codex = Harness(
            id="codex",
            command="codex",
            prompt_mode="arg",
            args=("--full-auto",),
            resume=ResumeSpec(style="subcommand", subcommand="resume"),
        )
        spec = _build(builder, harness=codex, resume_key="k1")
        assert list(spec.command)[:3] == ["codex", "resume", "k1"]
        # ...and the harness's own args still follow.
        assert "--full-auto" in spec.command

    def test_harness_args_are_preserved_in_order(self, builder):
        harness = replace(CLAUDE, args=("--a", "--b"))
        argv = list(_build(builder, harness=harness).command)
        assert argv.index("--a") < argv.index("--b")


class TestPromptDelivery:
    def test_mode_arg_puts_the_prompt_last(self, builder):
        spec = _build(builder)
        assert spec.prompt_mode == "arg"
        assert spec.command[-1] == spec.prompt
        assert spec.files == ()

    def test_mode_flag_puts_the_flag_immediately_before_the_prompt(self, builder):
        harness = replace(CLAUDE, prompt_mode="flag", prompt_flag="--prompt")
        spec = _build(builder, harness=harness)
        argv = list(spec.command)
        assert argv[-2] == "--prompt"
        assert argv[-1] == spec.prompt

    def test_mode_none_delivers_no_prompt_at_all(self, builder):
        harness = replace(CLAUDE, prompt_mode="none")
        spec = _build(builder, harness=harness)
        assert spec.prompt is None
        assert spec.command[-1] != ""
        assert "aq prime" not in " ".join(spec.command)

    def test_bootstrap_prompt_is_short_and_names_the_protocol(self, builder):
        spec = _build(builder)
        assert len(spec.prompt) < 600  # short on purpose
        assert "aq prime" in spec.prompt
        assert "aq task close" in spec.prompt
        assert "aq session drain-ack" in spec.prompt
        assert "task-1" in spec.prompt

    def test_oversized_prompt_moves_to_a_file(self, builder):
        big = "x" * 5000
        spec = _build(builder, prompt=big)
        assert spec.prompt == big
        # The prompt itself is nowhere in argv.
        assert big not in spec.command
        rel, content = spec.files[0]
        assert rel.startswith(".aq/tmp/") and content == big

    def test_oversized_prompt_argv_stays_small_and_execs_the_harness(self, builder):
        big = "x" * 20000
        spec = _build(builder, prompt=big)
        argv = list(spec.command)
        assert argv[0] == "sh" and argv[1] == "-c"
        assert "exec" in argv[2]
        # tmux's new-session command buffer is ~2 KB -- the whole argv must
        # stay far under it however long the prompt gets.
        assert len(" ".join(argv)) < 1024
        assert "claude" in argv

    def test_oversized_prompt_under_flag_mode_keeps_the_flag(self, builder):
        harness = replace(CLAUDE, prompt_mode="flag", prompt_flag="--prompt")
        spec = _build(builder, harness=harness, prompt="y" * 5000)
        assert list(spec.command)[-1] == "--prompt"

    def test_threshold_is_bytes_not_characters(self, builder):
        """A prompt of multi-byte characters must count as its byte length."""
        harness = replace(CLAUDE, max_argv_prompt_bytes=100)
        # 60 characters, 180 bytes.
        spec = _build(builder, harness=harness, prompt="é" * 60)
        assert spec.files, "multi-byte prompt should have overflowed to a file"

    def test_exactly_at_the_threshold_still_rides_argv(self, builder):
        harness = replace(CLAUDE, max_argv_prompt_bytes=100)
        spec = _build(builder, harness=harness, prompt="z" * 100)
        assert spec.files == ()


class TestEnvMarkers:
    def test_all_nine_markers_present_for_a_task_session(self, builder):
        spec = _build(builder)
        for key in AQ_MARKER_KEYS:
            assert key in spec.env, key
        assert spec.env["AQ_SESSION_ID"] == "sess-abc"
        assert spec.env["AQ_TASK_ID"] == "task-1"
        assert spec.env["AQ_PROJECT_ID"] == "proj-1"
        assert spec.env["AQ_PROFILE"] == "claude-opus"
        assert spec.env["AQ_DAEMON_EPOCH"] == "epoch-1"
        assert spec.env["AQ_INSTANCE_TOKEN"] == "tok-1"
        assert spec.env["AQ_WORK_DIR"] == "/wd"

    def test_aq_task_id_matches_the_name_the_cli_falls_back_to(self, builder):
        """The other half of ``aq prime`` / ``aq handoff``'s handshake."""
        import inspect

        from src.cli import agent_surface

        source = inspect.getsource(agent_surface)
        assert 'os.environ.get("AQ_TASK_ID")' in source
        assert 'os.environ.get("AQ_SESSION_ID")' in source
        spec = _build(builder)
        assert "AQ_TASK_ID" in spec.env and "AQ_SESSION_ID" in spec.env

    def test_startup_prompt_delivered_is_set_when_the_prompt_rode_argv(self, builder):
        spec = _build(builder)
        assert spec.env[STARTUP_PROMPT_DELIVERED] == "1"

    def test_startup_prompt_delivered_is_absent_for_prompt_mode_none(self, builder):
        harness = replace(CLAUDE, prompt_mode="none")
        spec = _build(builder, harness=harness)
        assert STARTUP_PROMPT_DELIVERED not in spec.env

    def test_named_session_omits_task_id_rather_than_setting_it_empty(self, builder):
        spec = builder.build_named_spec(
            profile=_Profile(id="supervisor"),
            harness=CLAUDE,
            project_id="proj-1",
            work_dir="/wd",
            session_id="s1",
            instance_token="t1",
        )
        assert "AQ_TASK_ID" not in spec.env
        assert spec.lifecycle == "named"
        assert spec.session_name == "n-supervisor--proj-1"

    def test_every_session_gets_database_isolation(self, builder):
        """A slot's db tooling points at scratch, never at config.yaml.

        Both halves matter: ``AQ_DB_SCOPE`` is what makes
        ``src.database.migration_guard`` refuse a migration against the
        production URL, and the two URL overrides mean the ordinary path
        never reaches the guard at all.  Regression for 2026-09-02, when a
        worker session stamped production with an unmerged revision.
        """
        env = _build(builder).env
        assert env["AQ_DB_SCOPE"] == "worker"
        assert env["AQ_DATABASE_URL"] == "/wd/.aq/scratch.db"
        assert env["AGENT_QUEUE_DB"] == env["AQ_DATABASE_URL"]

    def test_a_harness_may_pin_its_own_database_url(self, builder):
        """Isolation is a default, not a cage: an explicit pin still wins."""
        harness = replace(CLAUDE, env=(("AGENT_QUEUE_DB", "/pinned.db"),))
        assert _build(builder, harness=harness).env["AGENT_QUEUE_DB"] == "/pinned.db"

    def test_harness_env_is_merged(self, builder):
        harness = replace(CLAUDE, env=(("MY_FLAG", "1"),))
        assert _build(builder, harness=harness).env["MY_FLAG"] == "1"

    def test_claudecode_is_stripped(self):
        env = build_session_env(
            session_id="s",
            task_id="t",
            project_id="p",
            profile_id="pr",
            epoch="e",
            instance_token="i",
            work_dir="/wd",
            api_url="http://x",
            api_token="tok",
            base={"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/usr/bin"},
        )
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE_ENTRYPOINT" not in env
        assert env["PATH"] == "/usr/bin"

    def test_a_harness_cannot_reintroduce_claudecode(self):
        """Explicit entries normally win -- this is the one exception."""
        env = build_session_env(
            session_id="s",
            task_id="t",
            project_id="p",
            profile_id="pr",
            epoch="e",
            instance_token="i",
            work_dir="/wd",
            api_url="http://x",
            api_token="tok",
            harness_env={"CLAUDECODE": "1"},
            base={},
        )
        assert "CLAUDECODE" not in env

    def test_session_excludes_all_inherited_harness_markers(self):
        env = build_session_env(
            session_id="s", task_id="t", project_id="p", profile_id="pr", epoch="e",
            instance_token="i", work_dir="/wd", api_url="http://x", api_token="tok",
            base={
                "CLAUDE_CODE_SESSION_ID": "parent", "CLAUDE_CODE_MESSAGING_SOCKET": "/x",
                "CLAUDE_PID": "123", "ANTHROPIC_AUTH_TOKEN": "session-token",
                "CODEX_SANDBOX": "seatbelt", "CODEX_CI": "1",
                "ANTHROPIC_API_KEY": "api-key", "CODEX_API_KEY": "codex-key",
            },
        )
        for key in (
            "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_PID",
            "ANTHROPIC_AUTH_TOKEN", "CODEX_SANDBOX", "CODEX_CI",
        ):
            assert key not in env
        assert env["ANTHROPIC_API_KEY"] == "api-key"
        assert env["CODEX_API_KEY"] == "codex-key"

    def test_scrub_withholds_daemon_secrets_but_keeps_harness_credentials(self):
        env = build_session_env(
            session_id="s",
            task_id="t",
            project_id="p",
            profile_id="pr",
            epoch="e",
            instance_token="i",
            work_dir="/wd",
            api_url="http://x",
            api_token="tok",
            base={
                "DISCORD_BOT_TOKEN": "secret",
                "DATABASE_DSN": "postgres://u:p@h/db",
                "ANTHROPIC_API_KEY": "sk-ant",
                "PATH": "/usr/bin",
            },
        )
        assert "DISCORD_BOT_TOKEN" not in env
        assert "DATABASE_DSN" not in env
        # An agent CLI that cannot authenticate is not a safer agent.
        assert env["ANTHROPIC_API_KEY"] == "sk-ant"
        # AQ_API_TOKEN is explicit, so it survives despite looking secret.
        assert env["AQ_API_TOKEN"] == "tok"

    def test_config_kill_switch_is_honoured_when_config_is_passed(self):
        class _Sec:
            env_scrub_enabled = False
            env_allowlist = ()

        class _C:
            security = _Sec()

        env = build_session_env(
            session_id="s",
            task_id="t",
            project_id="p",
            profile_id="pr",
            epoch="e",
            instance_token="i",
            work_dir="/wd",
            api_url="http://x",
            api_token="tok",
            config=_C(),
            base={"DISCORD_BOT_TOKEN": "secret"},
        )
        # With the switch off only STRIP_ALWAYS applies -- which is exactly
        # what "kill switch" has to mean for it to be a rollback.
        assert env["DISCORD_BOT_TOKEN"] == "secret"

    def test_spec_builder_passes_config_into_the_scrub(self, builder):
        """1C's bug was calling the scrub without a config; pin the fix."""
        import inspect

        from src.sessions import spec as spec_mod

        source = inspect.getsource(spec_mod.SessionSpecBuilder._build)
        assert "config=self.config" in source


class TestHookMaterial:
    def test_hook_templates_are_rendered_into_the_work_dir(self, builder):
        harness = replace(
            CLAUDE,
            supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
        )
        spec = _build(builder, harness=harness)
        by_path = dict(spec.files)
        assert ".aq/hooks/claude.json" in by_path
        content = by_path[".aq/hooks/claude.json"]
        assert "SessionStart" in content and "aq prime --hook-json" in content
        assert "PreCompact" in content and "aq handoff --auto" in content
        # No Stop hook: completion is explicit, and a Stop hook would
        # re-introduce exit-as-signal.
        assert '"Stop"' not in content

    def test_no_hook_files_when_the_harness_does_not_support_hooks(self, builder):
        harness = replace(
            CLAUDE,
            supports_hooks=False,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
        )
        assert _build(builder, harness=harness).files == ()

    def test_a_missing_template_is_skipped_not_fatal(self, builder):
        harness = replace(
            CLAUDE, supports_hooks=True, hook_files=((".aq/x.json", "hooks/ghost.json"),)
        )
        spec = _build(builder, harness=harness)
        assert spec.files == ()  # launch still proceeds

    def test_hook_payload_contains_the_four_shipped_hook_events(self, builder):
        """SessionStart, PreCompact and the two subagent halves.

        No Stop, and no UserPromptSubmit.  Design §3.8 declared the latter:
        ``aq inbox --inject`` at every prompt boundary.  It was removed
        2026-08-27 — see the class of test below.
        """
        import json as _json

        harness = replace(
            CLAUDE,
            supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
        )
        spec = _build(builder, harness=harness)
        by_path = dict(spec.files)
        payload = _json.loads(by_path[".aq/hooks/claude.json"])
        hooks = payload["hooks"]
        assert set(hooks.keys()) == {
            "SessionStart", "PreCompact", "SubagentStart", "SubagentStop",
        }, hooks.keys()

    def test_the_subagent_halves_both_report_to_one_receiver(self, builder):
        """Start and stop must land in the same place or they cannot pair."""
        import json as _json

        harness = replace(
            CLAUDE, supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
        )
        payload = _json.loads(
            dict(_build(builder, harness=harness).files)[".aq/hooks/claude.json"]
        )
        for event in ("SubagentStart", "SubagentStop"):
            entry = payload["hooks"][event][0]["hooks"][0]
            assert entry["type"] == "command"
            assert entry["command"] == "aq subagent event --hook-json"
            # Short: this runs on the agent's critical path, twice per child.
            assert entry["timeout"] == 10
        # No matcher — every sub-agent type counts, not a curated subset.
        assert "matcher" not in payload["hooks"]["SubagentStart"][0]

    def test_session_start_hook_runs_aq_prime(self, builder):
        import json as _json

        harness = replace(
            CLAUDE, supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
        )
        payload = _json.loads(
            dict(_build(builder, harness=harness).files)[".aq/hooks/claude.json"]
        )
        entries = payload["hooks"]["SessionStart"]
        # Nested {"hooks":[{"type":"command","command":"...","timeout":30}]}
        cmd_entry = entries[0]["hooks"][0]
        assert cmd_entry["type"] == "command"
        assert "aq prime" in cmd_entry["command"]
        # Design §3.8: SessionStart hook wants 30s to render the prompt.
        assert cmd_entry["timeout"] == 30

    def test_precompact_hook_writes_handoff_no_restart(self, builder):
        import json as _json

        harness = replace(
            CLAUDE, supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
        )
        payload = _json.loads(
            dict(_build(builder, harness=harness).files)[".aq/hooks/claude.json"]
        )
        cmd_entry = payload["hooks"]["PreCompact"][0]["hooks"][0]
        assert "aq handoff" in cmd_entry["command"]
        # ``--auto`` = write note, do not restart (gc-flp1 scar).
        assert "--auto" in cmd_entry["command"]
        assert cmd_entry["timeout"] == 30

    def test_no_per_prompt_hook(self, builder):
        """``aq inbox --inject`` on every UserPromptSubmit is gone.

        It cost ~1.3 s of interpreter startup per prompt and delivered
        nothing: ``aq inbox`` is a Phase S1 stub that returns immediately
        (``src/cli/agent_surface.py``), and the real command was never wired
        to it.  Meanwhile all three designed delivery paths work -- the
        cascade's nudge, prime (``via="prime"``), and the transcript-tail
        fallback.

        This is the same per-turn shell-out the 2026-08-19 Gas City
        comparison listed among *their* weaknesses; we had adopted the
        mechanism without answering the criticism.  Prompt-boundary
        injection has to be measured against nudge before it comes back.
        """
        harness = replace(
            CLAUDE, supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
        )
        raw = dict(_build(builder, harness=harness).files)[".aq/hooks/claude.json"]
        assert "UserPromptSubmit" not in raw
        assert "aq inbox" not in raw

    def test_no_stop_hook_in_shipped_payload(self, builder):
        """Design §3.8: no Stop hook — completion is explicit."""
        harness = replace(
            CLAUDE, supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
        )
        raw = dict(_build(builder, harness=harness).files)[".aq/hooks/claude.json"]
        assert '"Stop"' not in raw

    def test_session_start_hook_matches_resume_and_compact_only(self, builder):
        """Design §3.8: SessionStart is suppressed when the bootstrap rode
        argv on this start; active after compaction and on resume.

        Claude Code's SessionStart supports ``startup | resume | compact``
        matchers.  Restricting to ``resume|compact`` implements the
        suppression: fresh starts already got the prompt through argv, so
        ``aq prime --hook-json`` on top would double-inject.
        """
        import json as _json

        harness = replace(
            CLAUDE, supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
        )
        payload = _json.loads(
            dict(_build(builder, harness=harness).files)[".aq/hooks/claude.json"]
        )
        matcher = payload["hooks"]["SessionStart"][0].get("matcher")
        assert matcher is not None, "SessionStart entry must declare a matcher"
        assert "resume" in matcher and "compact" in matcher
        assert "startup" not in matcher

    def test_hook_payload_is_valid_json(self, builder):
        import json as _json

        harness = replace(
            CLAUDE, supports_hooks=True,
            hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
        )
        raw = dict(_build(builder, harness=harness).files)[".aq/hooks/claude.json"]
        # Must round-trip cleanly — a broken template breaks every launch.
        _json.loads(raw)


class TestSpecShape:
    def test_readiness_and_process_hints_come_from_the_harness(self, builder):
        harness = replace(
            CLAUDE, ready_delay_ms=2500, ready_prompt_prefix="> ", skip_escape_before_enter=False
        )
        spec = _build(builder, harness=harness)
        assert spec.ready_delay_ms == 2500
        assert spec.ready_prompt_prefix == "> "
        assert spec.process_names == ("claude", "node")
        assert spec.skip_escape_before_enter is False

    def test_spec_is_frozen(self, builder):
        spec = _build(builder)
        with pytest.raises(Exception):
            spec.session_name = "other"

    def test_instance_token_rides_the_spec(self, builder):
        assert _build(builder).instance_token == "tok-1"

    def test_default_api_url_falls_back_to_the_mcp_endpoint(self, builder):
        spec = builder.build_task_spec(
            task=_Task(),
            profile=_Profile(),
            harness=CLAUDE,
            work_dir="/wd",
            session_id="s",
            instance_token="t",
        )
        assert spec.env["AQ_API_URL"] == "http://127.0.0.1:8081"

    def test_wildcard_bind_is_rewritten_to_loopback(self):
        class _Wild:
            host = "0.0.0.0"
            port = 9000

        class _C:
            mcp_server = _Wild()
            security = None

        b = SessionSpecBuilder(_C())
        spec = b.build_task_spec(
            task=_Task(),
            profile=_Profile(),
            harness=CLAUDE,
            work_dir="/wd",
            session_id="s",
            instance_token="t",
        )
        assert spec.env["AQ_API_URL"] == "http://127.0.0.1:9000"
