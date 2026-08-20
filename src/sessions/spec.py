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
    "BOOTSTRAP_PROMPT",
]

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")

#: The bootstrap prompt.  Deliberately tiny — see the module docstring.
#: ``{}`` fields: task_id, work_dir.
BOOTSTRAP_PROMPT = (
    "You are running task {task_id} in {work_dir}.\n"
    "Run `aq prime` first and follow what it tells you.\n"
    "When the work is done, close the task explicitly:\n"
    "  aq task close {task_id} --outcome pass --work-outcome shipped\n"
    "  aq session drain-ack\n"
    "Exiting without `aq task close` is treated as a failure, not a success."
)

#: Bootstrap for a named (persistent) session — no task in scope.
NAMED_BOOTSTRAP_PROMPT = (
    "You are the {profile} session in {work_dir}.\n"
    "Run `aq prime` first and follow what it tells you.\n"
    "Work arrives as messages; stay running and wait for it."
)

#: The ``sh -c`` script used for oversized prompts.  ``$1`` is the prompt
#: file; everything after it is the harness argv.
_PROMPT_FILE_SCRIPT = '__aq_prompt=$(cat "$1"); shift; exec "$@" "$__aq_prompt"'


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


class SessionSpecBuilder:
    """Builds :class:`SessionSpec` objects.  Stateless apart from config."""

    def __init__(self, config, harnesses=None):
        self.config = config
        self.harnesses = harnesses

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
    ) -> SessionSpec:
        """Spec for a one-task session (``lifecycle="task"``)."""
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
        name = named_session_name(profile_id, project_id)
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
    ) -> SessionSpec:
        files: list[tuple[str, str]] = []

        prompt = None if harness.prompt_mode == "none" else bootstrap
        argv = self._compose_argv(
            harness=harness,
            profile=profile,
            session_id=session_id,
            resume_key=resume_key,
            prompt=prompt,
            session_name=session_name,
            files=files,
        )

        files.extend(self._hook_files(harness))

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
    ) -> list[str]:
        argv: list[str] = [harness.command]

        # Resume, style "subcommand", goes immediately after the command
        # (``codex resume <key>``) — before any flags, because a subcommand
        # that follows a flag is a different CLI grammar.
        if resume_key and harness.resume.style == "subcommand":
            argv.append(harness.resume.subcommand)
            argv.append(resume_key)

        argv.extend(harness.args)

        model = (getattr(profile, "model", "") or "").strip()
        if model and harness.model_flag:
            argv.extend([harness.model_flag, model])

        effort = (getattr(profile, "effort", "") or "").strip()
        if effort and harness.effort_flag:
            argv.extend([harness.effort_flag, effort])

        if harness.permission_flag:
            argv.append(harness.permission_flag)

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
