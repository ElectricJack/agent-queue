"""The engine's own steps: host admission and prerequisite detection.

These are the steps that belong to no adapter — they are true of every
supported host and every selected capability.  Everything that installs
something (WSL, Homebrew, PostgreSQL, a provider CLI) is registered by the
adapter that owns it; this module deliberately stops at *detection* plus the
one directory AQ cannot run without.

The factories here are the reference implementation of the step protocol:
:func:`command_step` shows a read-only prerequisite with a remediation, and
:func:`data_directory_step` shows a mutating step that records an owned
resource and re-verifies instead of recreating it.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from .results import ResourceRecord, StepResult
from .state import default_state_dir
from .steps import StepContext, StepRegistry, StepSpec

#: The interpreter floor, mirroring ``requires-python`` in ``pyproject.toml``.
MINIMUM_PYTHON = (3, 12)

STEP_HOST = "host.supported"
STEP_PYTHON = "prereq.python"
STEP_GIT = "prereq.git"
STEP_TMUX = "prereq.tmux"
STEP_DATA_DIR = "prereq.data-dir"


def host_step() -> StepSpec:
    """Record the matrix verdict as a step.

    The engine already refuses an unsupported host before any step runs; this
    exists so the *record* carries the verdict — a resume record that cannot
    say which host it was written for is not much of a record.
    """

    def run(context: StepContext) -> StepResult:
        verdict = context.support
        detail = verdict.to_dict()
        if verdict.notes:
            return StepResult.succeeded(
                STEP_HOST,
                f"{verdict.host_path} ({verdict.tier} tier): {verdict.notes[0]}",
                detail=detail,
            )
        return StepResult.succeeded(
            STEP_HOST,
            f"{verdict.host_path} ({verdict.tier} tier)",
            detail=detail,
        )

    return StepSpec(
        id=STEP_HOST,
        title="Confirm the host is supported",
        description="Places the observed OS, version, architecture and WSL generation in the "
        "supported-platform matrix.",
        run=run,
        verify=lambda context: context.support.installable,
    )


def python_step(
    *,
    version_info: tuple[int, ...] | None = None,
    depends_on: tuple[str, ...] = (STEP_HOST,),
) -> StepSpec:
    """Check the interpreter running the installer."""
    observed = tuple(version_info or sys.version_info[:3])

    def run(context: StepContext) -> StepResult:
        rendered = ".".join(str(part) for part in observed)
        if observed[:2] >= MINIMUM_PYTHON:
            return StepResult.succeeded(
                STEP_PYTHON,
                f"Python {rendered}",
                detail={"version": rendered, "executable": sys.executable},
            )
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        return StepResult.failed(
            STEP_PYTHON,
            f"Python {rendered} is older than the required {required}",
            f"Install Python {required} or newer and rerun `aq install` with that interpreter.",
            detail={"version": rendered, "required": required},
            retryable=False,
        )

    return StepSpec(
        id=STEP_PYTHON,
        title="Check the Python interpreter",
        description=f"AQ requires Python {'.'.join(str(p) for p in MINIMUM_PYTHON)} or newer.",
        run=run,
        depends_on=depends_on,
    )


def command_step(
    step_id: str,
    command: str,
    *,
    title: str,
    remediation: Mapping[str, str] | str,
    description: str = "",
    capability: str | None = None,
    which: Callable[[str], str | None] | None = None,
    depends_on: tuple[str, ...] = (STEP_HOST,),
) -> StepSpec:
    """Build a read-only "is this executable on PATH?" prerequisite step.

    *remediation* may be a single string or a mapping keyed by host path
    (``windows-wsl2``, ``macos-apple-silicon``, …) with a ``default`` fallback,
    because "install tmux" is a different command on Ubuntu and on macOS and a
    generic instruction helps nobody.
    """
    lookup = which or shutil.which

    def _advice(host_path: str) -> str:
        if isinstance(remediation, str):
            return remediation
        return remediation.get(host_path) or remediation.get("default") or ""

    def run(context: StepContext) -> StepResult:
        resolved = lookup(command)
        if resolved:
            return StepResult.succeeded(
                step_id,
                f"{command} found at {resolved}",
                detail={"command": command, "path": resolved},
                resources=(
                    ResourceRecord(
                        kind="command",
                        id=command,
                        owned=False,
                        reused=True,
                        detail={"path": resolved},
                    ),
                ),
            )
        return StepResult.failed(
            step_id,
            f"{command} is not on PATH",
            _advice(context.support.host_path),
            detail={"command": command},
        )

    return StepSpec(
        id=step_id,
        title=title,
        description=description,
        run=run,
        depends_on=depends_on,
        capability=capability,
        verify=lambda context: lookup(command) is not None,
    )


def git_step(
    *,
    which: Callable[[str], str | None] | None = None,
    depends_on: tuple[str, ...] = (STEP_HOST,),
) -> StepSpec:
    return command_step(
        STEP_GIT,
        "git",
        title="Check Git",
        description="AQ clones, branches and delivers work with Git on the daemon host.",
        remediation={
            "windows-wsl2": "Install Git inside WSL with `sudo apt-get install -y git`.",
            "default": "Install Git (macOS: `xcode-select --install` or `brew install git`).",
        },
        which=which,
        depends_on=depends_on,
    )


def tmux_step(
    *,
    which: Callable[[str], str | None] | None = None,
    depends_on: tuple[str, ...] = (STEP_HOST,),
) -> StepSpec:
    return command_step(
        STEP_TMUX,
        "tmux",
        title="Check tmux",
        description="Every agent harness runs inside a tmux session, so tmux is required "
        "on the daemon host.",
        remediation={
            "windows-wsl2": "Install tmux inside WSL with `sudo apt-get install -y tmux`.",
            "default": "Install tmux (macOS: `brew install tmux`).",
        },
        which=which,
        depends_on=depends_on,
    )


def data_directory_step(
    *,
    environ: Mapping[str, str] | None = None,
    path: Path | None = None,
) -> StepSpec:
    """Ensure AQ's data directory exists and is writable.

    Mutating, and therefore consent-gated — but idempotent twice over: the
    verifier short-circuits a rerun, and the run itself distinguishes a
    directory it created (owned) from one that was already there (reused), so
    repeating the install never re-owns an operator's existing directory.
    """
    resolved = path or default_state_dir(environ)

    def _writable(target: Path) -> bool:
        return target.is_dir() and os.access(target, os.W_OK)

    def run(context: StepContext) -> StepResult:
        existed = resolved.exists()
        if existed and not resolved.is_dir():
            return StepResult.failed(
                STEP_DATA_DIR,
                f"{resolved} exists and is not a directory",
                f"Move or remove {resolved}, then rerun `aq install`.",
                retryable=True,
            )
        if not existed:
            try:
                resolved.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                return StepResult.failed(
                    STEP_DATA_DIR,
                    f"could not create {resolved}: {error}",
                    f"Create {resolved} with write permission for this user, then rerun "
                    "`aq install`.",
                )
        if not _writable(resolved):
            return StepResult.failed(
                STEP_DATA_DIR,
                f"{resolved} is not writable by this user",
                f"Grant this user write access to {resolved} (for example `chown -R "
                f"$USER {resolved}`), then rerun `aq install`.",
            )
        return StepResult.succeeded(
            STEP_DATA_DIR,
            f"{'using existing' if existed else 'created'} {resolved}",
            detail={"path": str(resolved), "created": not existed},
            resources=(
                ResourceRecord(
                    kind="directory",
                    id=str(resolved),
                    owned=not existed,
                    reused=existed,
                ),
            ),
        )

    return StepSpec(
        id=STEP_DATA_DIR,
        title="Prepare the AQ data directory",
        description=f"Creates {resolved} when it does not exist and confirms it is writable.",
        run=run,
        depends_on=(STEP_HOST,),
        mutating=True,
        consent_prompt=f"Create {resolved} if it does not exist?",
        verify=lambda context: _writable(resolved),
    )


def default_registry(
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    state_dir: Path | None = None,
) -> StepRegistry:
    """The engine's built-in steps, in the order they run.

    Platform, packaging, database and provider adapters extend this registry
    with :meth:`StepRegistry.register`; they never replace it, so every install
    on every host starts from the same admission and prerequisite checks.
    :func:`src.install.registry.build_registry` is what composes those steps
    with the adapter for the host actually being installed, and is what
    ``aq install`` runs.
    """
    return StepRegistry(
        (
            host_step(),
            python_step(),
            git_step(which=which),
            tmux_step(which=which),
            data_directory_step(environ=environ, path=state_dir),
        )
    )
