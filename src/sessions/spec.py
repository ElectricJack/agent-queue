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
    class-driven override" and falls back to ``profile.model`` unchanged.
    """
    mapping = {"claude": "anthropic", "codex": "openai", "gemini": "google"}
    key = getattr(harness, "id", "") or getattr(harness, "command", "")
    return mapping.get(key, "")


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
    """Provider name for a pool worker session — also its ``session_id``.

    Unlike task and named sessions, a pool session has no separate id/name
    split: ``p-<profile>--<project>--<nonce>`` is both (swarm-work-model
    §11.2), so adoption and ``list_sessions`` can key on either.
    """
    return f"p-{sanitize_name(profile_id)}--{sanitize_name(project_id)}--{sanitize_name(nonce)}"


class SessionSpecBuilder:
    """Builds :class:`SessionSpec` objects.  Stateless apart from config."""

    def __init__(self, config, harnesses=None, *, intelligence_classes=None):
        self.config = config
        self.harnesses = harnesses
        # ``{class_id: IntelligenceClass}`` — empty by default so callers that
        # do not care about class-driven model overrides work unchanged.
        # Populated by the orchestrator from ``load_intelligence_classes``.
        self._intelligence_classes = intelligence_classes or {}

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
        instance_token: str,
        epoch: str = "",
        api_url: str = "",
        api_token: str = "",
        resume_key: str | None = None,
        prompt: str | None = None,
        workspace_source_type=None,
    ) -> SessionSpec:
        """Spec for a pool worker session (``lifecycle="pool"``, §11).

        *session_id* **is** the provider name — :func:`pool_session_name`
        already derived it, so there is no separate id/name split to
        reconcile the way task and named sessions have.  The bootstrap
        prompt and env markers identify the launch as a pool worker rather
        than a one-task or persistent session, per §11.2/§11.3.
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
            session_name=session_id,
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

        # Resolve the model: intelligence class (task-scoped, then profile
        # default) wins over ``profile.model`` when it maps a slice for this
        # harness's provider.  Unknown class or missing mapping falls back
        # silently to the profile — a class typo must never break launch.
        model = self._resolve_model(
            profile, harness, task_intelligence_class, class_config=class_config
        )
        if model and harness.model_flag:
            argv.extend([harness.model_flag, model])

        effort = self._resolve_effort(profile, harness, class_config)
        if effort and harness.effort_flag:
            argv.extend([harness.effort_flag, effort])

        if harness.id == "codex" and Path(harness.command).name in {"codex", "codex.exe"}:
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
                argv.append(harness.permission_flag)
            else:
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
    _HARNESS_TOOL_NAMES: frozenset[str] = frozenset(
        {
            "Bash", "Read", "Write", "Edit", "Glob", "Grep", "Skill",
            "WebSearch", "WebFetch", "Task", "TodoWrite", "NotebookEdit",
        }
    )

    def _resolve_allowed_tools(self, profile, harness) -> list[str]:
        """Tool names to pass to the harness's allowlist flag.

        Returns an empty list to mean "emit no flag", which leaves the CLI on
        its own defaults.  That is the answer for three distinct cases: the
        profile declares nothing, it declares ``["*"]`` (explicitly all), or
        the harness has no ``tools_flag`` and therefore cannot be restricted.

        ``aq`` command names in the allowlist are dropped, with a log line
        naming them.  They are not tools the CLI knows: a session gets its
        daemon access through the ``aq`` CLI, so those names describe
        *intent* that the harness cannot enforce.  Dropping them silently is
        what let profiles carry allowlists nobody applied.
        """
        declared = list(getattr(profile, "allowed_tools", None) or [])
        if not declared:
            return []
        if "*" in declared:
            return []
        if not harness.tools_flag:
            logger.info(
                "harness %r has no tools_flag; profile %r allowlist not enforced",
                harness.id,
                getattr(profile, "id", "?"),
            )
            return []

        keep = [t for t in declared if t in self._HARNESS_TOOL_NAMES]
        dropped = [t for t in declared if t not in self._HARNESS_TOOL_NAMES]
        if dropped:
            logger.debug(
                "profile %r: %d allowlist entr(ies) are aq commands, not harness "
                "tools, and reach the session through the aq CLI instead: %s",
                getattr(profile, "id", "?"),
                len(dropped),
                ", ".join(sorted(dropped)),
            )
        if not keep:
            # Every entry was an aq command.  Emitting an empty flag would
            # disable *all* tools including Bash, which is how the session
            # reaches aq in the first place — that would strand the agent.
            logger.warning(
                "profile %r declares no harness tools (only aq commands); "
                "leaving the CLI on its defaults rather than disabling every tool",
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
        """Pick a fixed worker model, selected class model, or profile fallback."""
        worker_model = getattr(profile, "_agent_model_override", None)
        if worker_model:
            return worker_model
        if class_config is None:
            class_config = self._resolve_class_config(profile, harness, task_intelligence_class)
        return str(class_config.get("model") or getattr(profile, "model", "") or "").strip()

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
        or model mappings leave the profile fallback intact.
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
                "intelligence-class '%s' not found; falling back to profile.model",
                class_id,
            )
            return {}
        provider = (
            getattr(harness, "provider", "") or _infer_provider_from_harness(harness)
        )
        if not provider:
            logger.warning(
                "intelligence-class '%s': could not infer provider for harness %r; "
                "falling back to profile.model",
                class_id,
                getattr(harness, "id", "") or getattr(harness, "command", ""),
            )
            return {}
        from src.intelligence_classes import resolve_class

        slice_ = resolve_class(cls, provider)
        model = str(slice_.get("model") or "").strip()
        if not model:
            logger.warning(
                "intelligence-class '%s' has no model for provider %r; "
                "falling back to profile.model",
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
