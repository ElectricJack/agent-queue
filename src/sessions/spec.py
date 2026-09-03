"""Compose a launch: profile + harness + task → :class:`SessionSpec`.

Everything harness-specific happens here so providers stay dumb: a provider
receives an argv, an env, a work_dir and a list of files to write, and knows
nothing about Claude, Codex, prompt modes or hooks.

Three things are easy to get wrong and are therefore explicit:

**Names are derived, and only here.** ``s-<task_id>`` for task sessions,
``n-<profile>[--<project>]`` for named ones, sanitized to
``^[a-zA-Z0-9_-]+$``.  Other layers address named sessions by their
*logical* name (``supervisor-<project_id>``); mapping logical → provider
name is this module's job alone (design §3).

**The bootstrap prompt is short on purpose.** It says who you are, where you
are, and "run ``aq prime``".  The full prompt renders to
``<work_dir>/.aq/prompt.md`` and comes back through ``aq prime``, so the
launch does not have to squeeze a 40 KB prompt through a command line.

**Prompts over ~1 KB never ride argv.**  tmux's ``new-session`` command
buffer is roughly 2 KB, and a truncated command is a launch that half-works.
Above the harness's ``max_argv_prompt_bytes`` the prompt is written to
``.aq/tmp/`` and the argv becomes a tiny ``sh -c`` wrapper that reads it
back and ``exec``s the harness — the command string stays constant-size
however long the prompt is.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from src.profiles.capabilities import HARNESS_TOOL_NAMES
from src.resources.limits import session_env_caps, wrap_session_argv
from src.sessions.env import build_session_env
from src.sessions.harness_parser import Harness
from src.sessions.provider import SessionSpec

logger = logging.getLogger(__name__)

__all__ = [
    "SessionSpecBuilder",
    "sanitize_name",
    "task_session_name",
    "named_session_name",
    "pool_session_name",
    "skip_permissions_allowed",
    "BYPASS_PERMISSION_MODE",
    "BOOTSTRAP_PROMPT",
    "POOL_BOOTSTRAP_PROMPT",
]

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

#: ``profile.permission_mode`` value that is an explicit opt-in to the
#: harness's skip-permissions flag outside an isolated worktree.  Same
#: vocabulary the Claude SDK runtime already uses
#: (:data:`src.profiles.parser.VALID_PERMISSION_MODES`), so an operator
#: writes one word in one place and both runtimes honour it.
BYPASS_PERMISSION_MODE = "bypassPermissions"

#: Profile-owned, harness-specific autonomous modes.  These are intentionally
#: separate from ``Harness.permission_flag``: Codex's permission flag disables
#: both approvals and sandboxing, whereas ``--full-auto`` keeps the workspace
#: sandbox.  Claude's flag overlaps its legacy permission flag, so argv
#: composition adds derived flags idempotently.
CODEX_FULL_AUTO_FLAG = "--full-auto"
CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"

#: The bootstrap prompt.  Deliberately tiny — see the module docstring.
#: ``{}`` fields: task_id, work_dir.
#:
#: The heartbeat line is load-bearing, not advice.  With the transcript
#: reader deferred the lease has exactly two feeds — the provider's
#: ``last_activity`` and an explicit ``aq task heartbeat`` — and on the
#: subprocess provider ``last_activity`` is log-file mtime.  A long quiet
#: tool call therefore climbs the stall ladder, and a provider without
#: ``Cap.NUDGE`` skips straight to interrupt+kill.  Nothing else in the
#: system tells the agent to heartbeat, so this prompt has to.
BOOTSTRAP_PROMPT = (
    "You are running task {task_id} in {work_dir}.\n"
    "Run `aq prime` first and follow what it tells you.\n"
    "Before any command that will run quiet for more than a few minutes "
    "(long builds, full test suites, large installs), call "
    "`aq task heartbeat {task_id}` — silence past the lease is read as a "
    "stall and the daemon will interrupt you.\n"
    "When the work is done, close the task explicitly:\n"
    "  aq task close {task_id} --outcome pass --work-outcome shipped\n"
    "  aq session drain-ack\n"
    "Exiting without `aq task close` is treated as a failure, not a success."
)


def skip_permissions_allowed(profile, workspace_source_type) -> bool:
    """Whether this launch may carry the harness's skip-permissions flag.

    [[design/trust-and-ops]] §4 is narrow about this: skip-permissions
    applies *"when — and only when — the session's ``work_dir`` is an
    isolated per-task worktree"*, and *"sessions outside an isolated
    worktree do not get skip-permissions by default; profiles must opt
    in."*  The trust argument is the bounded blast radius of a disposable
    worktree, so a session running in the operator's real checkout (today's
    common case — worktree execution is a later lane, and most workspaces
    are ``LINK``) does not get to borrow it.

    Two ways to qualify:

    * the workspace is a git worktree (``RepoSourceType.WORKTREE``), or
    * the profile sets ``permission_mode: bypassPermissions``, which is the
      explicit opt-in §4 asks for.
    """
    value = getattr(workspace_source_type, "value", workspace_source_type)
    if isinstance(value, str) and value.lower() == "worktree":
        return True
    mode = str(getattr(profile, "permission_mode", "") or "").strip()
    return mode == BYPASS_PERMISSION_MODE

#: Bootstrap for a named (persistent) session — no task in scope.
#:
#: Deliberately does **not** say "run `aq prime`" the way
#: :data:`BOOTSTRAP_PROMPT` does.  ``prime`` renders *a task's* startup
#: document and requires a ``task_id`` (``--task-id``, defaulting to
#: ``$AQ_TASK_ID``); a named session has no task, so the very first thing
#: the supervisor did on every cold start was fail with "task_id is required
#: (no task in scope)" and then improvise.  ``aq inbox`` is the equivalent
#: that works here: it needs no task and surfaces exactly what a named
#: session exists to process.
NAMED_BOOTSTRAP_PROMPT = (
    "You are the {profile} session in {work_dir}.\n"
    "Check `aq inbox` for pending messages and handle anything waiting.\n"
    "Work arrives as messages; stay running and wait for it. "
    "Do not run `aq prime` — it is task-scoped and there is no task here."
)

#: Bootstrap for a pool worker session (swarm-work-model §11.3) — no task in
#: scope at launch; the worker claims its own work in a loop.  ``{}`` fields:
#: project_name, profile_id.
POOL_BOOTSTRAP_PROMPT = (
    "You are a pool worker for project {project_name} (profile {profile_id}).\n"
    "Loop: run `aq task claim --next --wait 60`. On `claimed`, run `aq prime`, do the\n"
    "work, then `aq task close --outcome pass|fail --summary \"...\" --claim-next --wait 60`.\n"
    "On `no_ready_work`, claim again. On `session_exhausted` or `drain_requested`, exit 0.\n"
    "On `not_admissible`, wait as instructed and claim again. Never touch tasks you do not\n"
    "hold; .aq/claim.json in your workspace is the proof of what you hold."
)

#: The ``sh -c`` script used for oversized prompts.  ``$1`` is the prompt
#: file; everything after it is the harness argv.
_PROMPT_FILE_SCRIPT = '__aq_prompt=$(cat "$1"); shift; exec "$@" "$__aq_prompt"'


def _infer_provider_from_harness(harness) -> str:
    """Infer a provider name from a harness's id/command.

    The provider name keys :attr:`IntelligenceClass.mapping`; when a harness
    file does not declare its provider explicitly, the CLI's identifier is a
    stable-enough proxy (design §2 — one harness = one CLI = one vendor).
    An unknown harness returns ``""``, which resolution treats as "no
    class-driven override" and leaves the launch model unset.
    """
    mapping = {"claude": "anthropic", "codex": "openai", "gemini": "google"}
    key = getattr(harness, "id", "") or getattr(harness, "command", "")
    return mapping.get(key, "")


def _is_codex_cli(harness) -> bool:
    """Keep Codex-specific settings off other harnesses using OpenAI models."""
    return (
        getattr(harness, "id", "") == "codex"
        and Path(getattr(harness, "command", "")).name in {"codex", "codex.exe"}
    )


def _is_claude_cli(harness) -> bool:
    """Keep Claude-specific settings off other Anthropic-backed harnesses."""
    return (
        getattr(harness, "id", "") == "claude"
        and Path(getattr(harness, "command", "")).name in {"claude", "claude.exe"}
    )


def _append_once(argv: list[str], flag: str) -> None:
    """Append a derived flag without disturbing operator-supplied argv."""
    if flag not in argv:
        argv.append(flag)


def sanitize_name(raw: str) -> str:
    """Fold *raw* into ``^[a-zA-Z0-9_-]+$``."""
    cleaned = _UNSAFE.sub("-", str(raw)).strip("-")
    return cleaned or "unnamed"


def task_session_name(task_id: str) -> str:
    """Provider name for a task session."""
    return f"s-{sanitize_name(task_id)}"


def named_session_name(profile_id: str, project_id: str | None = None) -> str:
    """Provider name for a named session.

    The ``--`` separator is why profile and project ids are sanitized
    first: an id containing ``--`` would make the name ambiguous to read,
    though nothing parses it back apart.
    """
    base = f"n-{sanitize_name(profile_id)}"
    if project_id:
        base = f"{base}--{sanitize_name(project_id)}"
    return base


def pool_session_name(profile_id: str, project_id: str, nonce: str) -> str:
    """Provider name for a pool worker session.

    Pool row IDs are UUIDs so harnesses such as Claude can receive them via
    ``--session-id``. This readable name remains the provider-facing address
    used for pool adoption and operator lookup.
    """
    return f"p-{sanitize_name(profile_id)}--{sanitize_name(project_id)}--{sanitize_name(nonce)}"


class SessionSpecBuilder:
    """Builds :class:`SessionSpec` objects.  Stateless apart from config."""

    def __init__(self, config, harnesses=None, *, intelligence_classes=None):
        self.config = config
        self.harnesses = harnesses
        # ``{class_id: IntelligenceClass}`` — empty by default so callers that
        # do not care about class-driven model overrides work unchanged.  The
        # orchestrator passes its live ``IntelligenceClassRegistry`` here, so
        # a class added to the vault is resolvable without a restart; hence
        # the ``is None`` test — an empty registry is falsy but must be kept.
        self._intelligence_classes = (
            intelligence_classes if intelligence_classes is not None else {}
        )

    # -- public API --------------------------------------------------------

    def build_task_spec(
        self,
        *,
        task,
        profile,
        harness: Harness,
        work_dir: str,
        session_id: str,
        instance_token: str,
        epoch: str = "",
        api_url: str = "",
        api_token: str = "",
        resume_key: str | None = None,
        prompt: str | None = None,
        workspace_source_type=None,
        extra_env: dict[str, str] | None = None,
    ) -> SessionSpec:
        """Spec for a one-task session (``lifecycle="task"``).

        *workspace_source_type* is the :class:`~src.models.RepoSourceType`
        of the workspace behind *work_dir*.  It is what
        :func:`skip_permissions_allowed` reads; omitting it is the safe
        default (no skip-permissions unless the profile opted in).

        *extra_env* is applied after the ``AQ_*`` markers, before scrubbing
        (see :func:`~src.sessions.env.build_session_env`) — e.g.
        ``AQ_CLAIM_EPOCH`` for a push launch that joins the claim fence
        (swarm-work-model §10).
        """
        name = task_session_name(task.id)
        bootstrap = prompt if prompt is not None else BOOTSTRAP_PROMPT.format(
            task_id=task.id, work_dir=work_dir
        )
        return self._build(
            harness=harness,
            profile=profile,
            session_name=name,
            work_dir=work_dir,
            session_id=session_id,
            task_id=task.id,
            project_id=task.project_id,
            profile_id=(getattr(profile, "id", "") or ""),
            instance_token=instance_token,
            epoch=epoch,
            api_url=api_url,
            api_token=api_token,
            resume_key=resume_key,
            bootstrap=bootstrap,
            lifecycle="task",
            allow_skip_permissions=skip_permissions_allowed(profile, workspace_source_type),
            task_intelligence_class=getattr(task, "intelligence_class", None),
            extra_env=extra_env,
        )

    def build_named_spec(
        self,
        *,
        profile,
        harness: Harness,
        project_id: str | None,
        work_dir: str,
        session_id: str,
        instance_token: str,
        epoch: str = "",
        api_url: str = "",
        api_token: str = "",
        wake: str = "fresh",
        resume_key: str | None = None,
        prompt: str | None = None,
    ) -> SessionSpec:
        """Spec for a persistent session (``lifecycle="named"``).

        ``wake="resume"`` only takes effect when a *resume_key* is actually
        known; waking "resume" with no key is a fresh start, not an error —
        the first wake of a never-run session has nothing to resume.
        """
        profile_id = getattr(profile, "id", "") or ""
        # Keep the established global runtime name for restart adoption; the
        # suffix is an address, not a project association.
        name = named_session_name(
            profile_id, "global" if profile_id == "supervisor" and project_id is None else project_id
        )
        effective_resume = resume_key if wake == "resume" else None
        bootstrap = prompt if prompt is not None else NAMED_BOOTSTRAP_PROMPT.format(
            profile=profile_id or "agent", work_dir=work_dir
        )
        return self._build(
            harness=harness,
            profile=profile,
            session_name=name,
            work_dir=work_dir,
            session_id=session_id,
            task_id=None,
            project_id=project_id or "",
            profile_id=profile_id,
            instance_token=instance_token,
            epoch=epoch,
            api_url=api_url,
            api_token=api_token,
            resume_key=effective_resume,
            bootstrap=bootstrap,
            lifecycle="named",
            # Named sessions have no workspace, so only the profile's
            # explicit ``permission_mode: bypassPermissions`` opt-in
            # (trust-and-ops §4) can grant the skip-permissions flag.
            allow_skip_permissions=skip_permissions_allowed(profile, None),
        )

    def build_pool_spec(
        self,
        *,
        profile,
        project,
        agent_id: str,
        harness: Harness,
        work_dir: str,
        session_id: str,
        session_name: str,
        instance_token: str,
        epoch: str = "",
        api_url: str = "",
        api_token: str = "",
        resume_key: str | None = None,
        prompt: str | None = None,
        workspace_source_type=None,
    ) -> SessionSpec:
        """Spec for a pool worker session (``lifecycle="pool"``, §11).

        *session_id* is the durable UUID that may be passed to a harness,
        while *session_name* is the readable provider-facing name from
        :func:`pool_session_name`. The bootstrap prompt and env markers
        identify the launch as a pool worker rather than a one-task or
        persistent session, per §11.2/§11.3.
        """
        profile_id = getattr(profile, "id", "") or ""
        project_id = getattr(project, "id", "") or ""
        project_name = getattr(project, "name", "") or project_id
        bootstrap = prompt if prompt is not None else POOL_BOOTSTRAP_PROMPT.format(
            project_name=project_name, profile_id=profile_id
        )
        extra_env = {
            "AQ_SESSION_KIND": "pool",
            "AQ_AGENT_ID": agent_id,
            "AQ_PROFILE_ID": profile_id,
            "GIT_AUTHOR_NAME": f"aq {profile_id}",
            "GIT_COMMITTER_NAME": f"aq {profile_id}",
            "GIT_AUTHOR_EMAIL": f"{profile_id}@agent-queue.local",
            "GIT_COMMITTER_EMAIL": f"{profile_id}@agent-queue.local",
        }
        return self._build(
            harness=harness,
            profile=profile,
            session_name=session_name,
            work_dir=work_dir,
            session_id=session_id,
            task_id=None,
            project_id=project_id,
            profile_id=profile_id,
            instance_token=instance_token,
            epoch=epoch,
            api_url=api_url,
            api_token=api_token,
            resume_key=resume_key,
            bootstrap=bootstrap,
            lifecycle="pool",
            allow_skip_permissions=skip_permissions_allowed(profile, workspace_source_type),
            extra_env=extra_env,
        )

    # -- internals ---------------------------------------------------------

    def _build(
        self,
        *,
        harness: Harness,
        profile,
        session_name: str,
        work_dir: str,
        session_id: str,
        task_id: str | None,
        project_id: str,
        profile_id: str,
        instance_token: str,
        epoch: str,
        api_url: str,
        api_token: str,
        resume_key: str | None,
        bootstrap: str,
        lifecycle: str,
        allow_skip_permissions: bool = False,
        task_intelligence_class: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> SessionSpec:
        files: list[tuple[str, str]] = []

        class_config = self._resolve_class_config(profile, harness, task_intelligence_class)
        hook_files = self._hook_files(harness)
        prompt = None if harness.prompt_mode == "none" else bootstrap
        argv = self._compose_argv(
            harness=harness,
            profile=profile,
            session_id=session_id,
            resume_key=resume_key,
            prompt=prompt,
            session_name=session_name,
            files=files,
            allow_skip_permissions=allow_skip_permissions,
            hook_files=hook_files,
            task_intelligence_class=task_intelligence_class,
            class_config=class_config,
        )

        files.extend(hook_files)
        # Mirrors the two argv branches above.  Recorded on the session row so
        # the flock can distinguish "no native subagents" from "we cannot see
        # native subagents" without re-reading a harness file that may have
        # changed since launch.
        hooks_provisioned = bool(hook_files) and (
            bool(harness.settings_flag)
            or (bool(harness.hook_trust_flag) and allow_skip_permissions)
        )

        launch_env = dict(extra_env or {})
        provider = getattr(harness, "provider", "") or _infer_provider_from_harness(harness)
        thinking = str(class_config.get("thinking") or "").strip()
        if provider == "anthropic":
            if thinking == "off":
                # Claude has no --effort off. This documented switch disables
                # thinking on Anthropic (except models such as Fable 5); on
                # third-party endpoints the provider may still enable it.
                launch_env["MAX_THINKING_TOKENS"] = "0"
            elif thinking in {"low", "medium", "high", "xhigh", "max"} and harness.effort_flag:
                # Claude's environment takes precedence over --effort. Keep
                # inherited daemon settings from overriding the chosen class.
                launch_env["CLAUDE_CODE_EFFORT_LEVEL"] = thinking

        # Resource gating layer 1 (docs/guides/resource-gating.md).  Passing
        # the harness's own env map as ``skip`` keeps an operator-pinned
        # value authoritative — the derived cap fills the gap, it does not
        # overrule a deliberate one.
        launch_env.update(
            session_env_caps(self.config, skip=getattr(harness, "env_map", None) or {})
        )
        argv = wrap_session_argv(argv, self.config, scope_name=session_name)

        env = build_session_env(
            session_id=session_id,
            task_id=task_id,
            project_id=project_id,
            profile_id=profile_id,
            epoch=epoch,
            instance_token=instance_token,
            work_dir=work_dir,
            api_url=api_url or self._default_api_url(),
            api_token=api_token,
            harness_env=harness.env_map,
            config=self.config,
            prompt_delivered=prompt is not None,
            extra_env=launch_env,
        )

        return SessionSpec(
            session_name=session_name,
            work_dir=work_dir,
            command=tuple(argv),
            env=env,
            prompt=prompt,
            prompt_mode=harness.prompt_mode,
            ready_delay_ms=harness.ready_delay_ms,
            ready_prompt_prefix=harness.ready_prompt_prefix,
            process_names=harness.process_names,
            lifecycle=lifecycle,
            dialogs=harness.dialogs,
            skip_escape_before_enter=harness.skip_escape_before_enter,
            files=tuple(files),
            instance_token=instance_token,
            hooks_provisioned=hooks_provisioned,
        )

    def _compose_argv(
        self,
        *,
        harness: Harness,
        profile,
        session_id: str,
        resume_key: str | None,
        prompt: str | None,
        session_name: str,
        files: list[tuple[str, str]],
        allow_skip_permissions: bool = False,
        hook_files: list[tuple[str, str]] | None = None,
        task_intelligence_class: str | None = None,
        class_config: dict | None = None,
    ) -> list[str]:
        if class_config is None:
            class_config = self._resolve_class_config(profile, harness, task_intelligence_class)
        argv: list[str] = [harness.command]

        # Resume, style "subcommand", goes immediately after the command
        # (``codex resume <key>``) — before any flags, because a subcommand
        # that follows a flag is a different CLI grammar.
        if resume_key and harness.resume.style == "subcommand":
            argv.append(harness.resume.subcommand)
            argv.append(resume_key)

        argv.extend(harness.args)

        # These profile switches are explicit opt-ins for one concrete CLI.
        # Raw harness args remain a supported escape hatch; when they already
        # carry the same flag, keep their position and do not append a second
        # copy.  False profile values likewise never remove raw args.
        # The established isolated-worktree / legacy bypass policy may add
        # Codex's stronger ``dangerously-bypass`` flag below.  Do not combine
        # it with ``--full-auto``: the two select conflicting approval and
        # sandbox modes, so the stronger backwards-compatible mode wins.
        codex_stronger_bypass = bool(allow_skip_permissions and harness.permission_flag)
        codex_full_auto = _is_codex_cli(harness) and bool(
            getattr(profile, "codex_full_auto", False)
        )
        if codex_full_auto:
            if codex_stronger_bypass:
                argv[:] = [arg for arg in argv if arg != CODEX_FULL_AUTO_FLAG]
            else:
                _append_once(argv, CODEX_FULL_AUTO_FLAG)

        claude_skip_permissions = _is_claude_cli(harness) and bool(
            getattr(profile, "claude_dangerously_skip_permissions", False)
        )
        if claude_skip_permissions:
            _append_once(argv, CLAUDE_DANGEROUSLY_SKIP_PERMISSIONS_FLAG)

        # Resolve the model from the task-scoped or profile default class.
        # Unknown classes or missing mappings leave the model unset; profiles
        # no longer carry a conflicting literal fallback.
        model = self._resolve_model(
            profile, harness, task_intelligence_class, class_config=class_config
        )
        if model and harness.model_flag:
            argv.extend([harness.model_flag, model])

        effort = self._resolve_effort(profile, harness, class_config)
        if effort and harness.effort_flag:
            argv.extend([harness.effort_flag, effort])

        if _is_codex_cli(harness):
            # Codex uses a TOML config override, not a generic effort flag.
            # Key and values verified against the installed CLI's generated
            # Config/ReasoningEffort schema and its --help configuration syntax.
            # https://developers.openai.com/codex/config-reference/
            reasoning = class_config.get("reasoning_effort")
            if reasoning is not None:
                if isinstance(reasoning, str) and reasoning in {
                    "none", "minimal", "low", "medium", "high", "xhigh",
                }:
                    argv.extend(["-c", f'model_reasoning_effort="{reasoning}"'])
                else:
                    logger.warning("Unsupported Codex reasoning effort %r; not applied", reasoning)

        tools = self._resolve_allowed_tools(profile, harness)
        if tools:
            argv.extend([harness.tools_flag, ",".join(tools)])

        # Declaring the flag in a harness file is *permission to use it*,
        # not an instruction to always use it -- see
        # :func:`skip_permissions_allowed` for the trust-and-ops §4 rule.
        if harness.permission_flag:
            if allow_skip_permissions:
                _append_once(argv, harness.permission_flag)
            elif harness.permission_flag not in argv:
                logger.debug(
                    "Session %s: withholding %s -- work_dir is not an isolated "
                    "worktree and the profile did not opt in",
                    session_name,
                    harness.permission_flag,
                )

        # The hook payload is only live if the harness is actually pointed at
        # it.  Writing the file and never passing the flag was a harness that
        # advertised ``supports_hooks: true`` and shipped a dead file.
        if hook_files and harness.settings_flag:
            argv.extend([harness.settings_flag, hook_files[0][0]])

        # Codex discovers ``<cwd>/.codex/hooks.json`` on its own -- there is no
        # settings flag to point it at -- but it refuses to *run* a hook file
        # it has not been shown in an interactive review screen (verified on
        # codex-cli 0.151.0: project ``trust_level = "trusted"`` is not
        # enough).  ``--dangerously-bypass-hook-trust`` is the CLI's own
        # documented escape hatch for "automation that already vets hook
        # sources", which is exactly this daemon writing its own file.  It
        # rides ``allow_skip_permissions`` rather than being unconditional:
        # where we would not hand the harness a sandbox bypass, we also do not
        # pre-trust hook files a checked-out repo might carry.
        if hook_files and harness.hook_trust_flag and allow_skip_permissions:
            argv.append(harness.hook_trust_flag)

        if resume_key and harness.resume.style == "flag":
            argv.extend([harness.resume.flag, resume_key])
        elif harness.session_id_flag and session_id and not resume_key:
            # Pin the harness's own session id to ours when it supports it,
            # so the transcript reader can find the file without guessing by
            # mtime.  Never combined with --resume: the resumed session
            # already has an id and passing both is a conflict.
            argv.extend([harness.session_id_flag, session_id])

        if prompt is None:
            return argv

        if harness.prompt_mode == "flag":
            argv.append(harness.prompt_flag)

        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes <= harness.max_argv_prompt_bytes:
            argv.append(prompt)
            return argv

        # Oversized: hand the prompt over by file.  ``.aq/tmp`` is inside
        # the work_dir so it is disposable with the worktree and visible to
        # whoever is debugging the session.
        rel = f".aq/tmp/prompt-{sanitize_name(session_name)}.txt"
        files.append((rel, prompt))
        logger.debug(
            "Session %s: prompt is %d bytes (> %d) — delivering via %s",
            session_name,
            prompt_bytes,
            harness.max_argv_prompt_bytes,
            rel,
        )
        return ["sh", "-c", _PROMPT_FILE_SCRIPT, "sh", rel, *argv]

    #: Tool names a CLI harness understands directly.  Anything else in a
    #: profile's allowlist is an ``aq`` *command* name, which reaches a
    #: session through the ``aq`` CLI (i.e. through ``Bash``) rather than as
    #: a tool the harness can be told about — sessions are launched without
    #: ``--mcp-config``, so there is no MCP tool surface to restrict.
    #:
    #: Defined once, in ``src/profiles/capabilities.py``, so the launcher's
    #: idea of a harness tool and the capability classifier's cannot drift.
    _HARNESS_TOOL_NAMES: frozenset[str] = HARNESS_TOOL_NAMES

    def _resolve_allowed_tools(self, profile, harness) -> list[str]:
        """Tool names to pass to the harness's allowlist flag.

        Reads the profile's ``harness_tools`` namespace via
        :func:`~src.profiles.capabilities.capability_policy_for`, so an
        authored ``## Capabilities`` block is what the launcher enforces and
        a legacy ``allowed_tools`` list is adapted without granting new
        rights.

        Returns an empty list to mean "emit no flag", which leaves the CLI on
        its own defaults.  Two cases reach that now: the harness has no
        ``tools_flag`` and therefore cannot be restricted, or the profile
        declares no harness tools at all.  ``["*"]`` is no longer one of
        them — a wildcard is rejected at parse and at sync (Playbook V2
        Package 0 §3.2), so it cannot reach this function.

        ``aq`` command names live in their own namespace and never reach the
        flag: a session gets its daemon access through the ``aq`` CLI, which
        the harness cannot gate.  The server-side check at dispatch is the
        boundary for those — see ``src/commands/authorization.py``.
        """
        from src.profiles.capabilities import capability_policy_for

        policy = capability_policy_for(profile)
        # ``getattr`` rather than attribute access: this is now reached for
        # every profile (the legacy adapter never yields an empty policy), so
        # a harness object without the field must degrade to "cannot
        # restrict" rather than raise mid-launch.
        if not getattr(harness, "tools_flag", ""):
            logger.info(
                "harness %r has no tools_flag; profile %r allowlist not enforced",
                getattr(harness, "id", "?"),
                getattr(profile, "id", "?"),
            )
            return []

        # Defensive filter: a legacy-derived policy classified its names with
        # no plugin registry wired, so an unrecognised entry could in
        # principle land here.  Keeping the log line preserves the old
        # diagnostic for profiles that have not migrated yet.
        keep = sorted(policy.harness_tools & self._HARNESS_TOOL_NAMES)
        dropped = sorted(policy.harness_tools - self._HARNESS_TOOL_NAMES)
        if dropped:
            logger.debug(
                "profile %r: %d harness_tools entr(ies) are not names this "
                "launcher recognises and were dropped: %s",
                getattr(profile, "id", "?"),
                len(dropped),
                ", ".join(dropped),
            )
        if not keep:
            # Emitting an empty flag would disable *all* tools including
            # Bash, which is how the session reaches aq in the first place —
            # that would strand the agent.  A profile that authored an empty
            # ``harness_tools`` alongside non-empty ``aq_commands`` is a
            # parse error (§3.2 rule 5), so reaching here means the profile
            # is a deliberate no-op or still on the legacy shape.
            logger.warning(
                "profile %r declares no harness tools; leaving the CLI on its "
                "defaults rather than disabling every tool",
                getattr(profile, "id", "?"),
            )
            return []
        return keep

    def _resolve_model(
        self,
        profile,
        harness,
        task_intelligence_class: str | None,
        *,
        class_config: dict | None = None,
    ) -> str:
        """Pick a fixed worker model or the selected intelligence-class model."""
        worker_model = getattr(profile, "_agent_model_override", None)
        if worker_model:
            return worker_model
        if class_config is None:
            class_config = self._resolve_class_config(profile, harness, task_intelligence_class)
        return str(class_config.get("model") or "").strip()

    def _resolve_effort(self, profile, harness, class_config: dict) -> str:
        """Only map thinking levels for the CLI whose semantics we support."""
        effort = str(getattr(profile, "effort", "") or "").strip()
        provider = getattr(harness, "provider", "") or _infer_provider_from_harness(harness)
        if provider == "anthropic":
            effort = str(class_config.get("thinking") or effort).strip()
            if effort == "off":
                return ""  # Applied separately through MAX_THINKING_TOKENS.
            if effort and effort not in {"low", "medium", "high", "xhigh", "max"}:
                logger.warning("Unsupported Claude thinking/effort value %r; not applied", effort)
                return ""
        # Other providers retain their explicit profile behavior. Codex class
        # reasoning uses its own config override in _compose_argv; a provider's
        # thinking_budget is not a generic CLI --effort argument either.
        if effort and not harness.effort_flag:
            logger.warning("Harness %r has no effort_flag; effort %r not applied", harness.id, effort)
        return effort

    def _resolve_class_config(self, profile, harness, task_intelligence_class) -> dict:
        """Resolve the whole provider slice without dropping its thinking fields.

        Worker class > task class > profile default. Unknown classes, providers,
        or model mappings leave launch settings empty rather than using a pin.
        """
        class_id = (
            getattr(profile, "_agent_intelligence_class", None)
            or task_intelligence_class
            or getattr(profile, "default_class", "")
            or ""
        )
        if not class_id:
            return {}
        cls = self._intelligence_classes.get(class_id)
        if cls is None:
            logger.warning(
                "intelligence-class '%s' not found; no launch model resolved",
                class_id,
            )
            return {}
        provider = (
            getattr(harness, "provider", "") or _infer_provider_from_harness(harness)
        )
        if not provider:
            logger.warning(
                "intelligence-class '%s': could not infer provider for harness %r; "
                "no launch model resolved",
                class_id,
                getattr(harness, "id", "") or getattr(harness, "command", ""),
            )
            return {}
        from src.intelligence_classes import resolve_class

        # Codex account models are a separate namespace from OpenAI API
        # defaults. The optional CLI slice never changes provider reporting or
        # the model chosen for another OpenAI harness.
        slice_ = (
            resolve_class(cls, "codex")
            if provider == "openai" and _is_codex_cli(harness) else {}
        )
        if not slice_:
            slice_ = resolve_class(cls, provider)
        model = str(slice_.get("model") or "").strip()
        if not model:
            logger.warning(
                "intelligence-class '%s' has no model for provider %r; "
                "no launch model resolved",
                class_id,
                provider,
            )
            return {}
        return slice_

    def _hook_files(self, harness: Harness) -> list[tuple[str, str]]:
        """Render the harness's declared hook templates.

        Payload *content* is owned by [[aq-surface]] (``src/prime/templates
        /hooks/``); which files get written, and when, is owned here.  A
        missing template is logged and skipped rather than failing the
        launch — a session without hooks still completes via ``aq task
        close``, which is the whole point of not depending on a Stop hook.
        """
        if not harness.supports_hooks or not harness.hook_files:
            return []
        root = Path(__file__).resolve().parent.parent / "prime" / "templates"
        out: list[tuple[str, str]] = []
        for dest, template in harness.hook_files:
            src = root / template
            try:
                out.append((dest, src.read_text(encoding="utf-8")))
            except OSError:
                logger.warning(
                    "Harness %s declares hook template %r which is missing at %s — skipped",
                    harness.id,
                    template,
                    src,
                )
        return out

    def _default_api_url(self) -> str:
        """Where ``aq`` inside the session should reach this daemon.

        Same source the CLI falls back to (``src/cli/client.py``): the
        embedded MCP server's host/port.  A wildcard bind is rewritten to
        loopback because the *session* dials it, and "0.0.0.0" is not a
        destination.
        """
        mcp = getattr(self.config, "mcp_server", None)
        host = getattr(mcp, "host", None) or "127.0.0.1"
        port = getattr(mcp, "port", None) or 8081
        if host in ("0.0.0.0", "::", "[::]"):
            host = "127.0.0.1"
        return f"http://{host}:{port}"
