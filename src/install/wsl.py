"""The WSL2 (Ubuntu) platform adapter: the prerequisites apt owns.

macOS has had an adapter that *installs* Git and tmux (``src/install/macos.py``)
since the first install epoch; WSL had none, so ``prereq.tmux`` on a fresh
Ubuntu failed with "install tmux inside WSL with `sudo apt-get install -y
tmux`".  tmux is not optional -- every agent harness runs inside a tmux session,
and ``sessions.provider: tmux`` is written only once that check passes -- so a
missing package turned the one-line install into a manual step, which is exactly
what the install epoch set out to remove.

This adapter is deliberately thin.  It installs only what the engine goes on to
check (``prereq.git``, ``prereq.tmux``), nothing that AQ can build itself (Node
comes from the pinned toolchain in ``node_toolchain.py``), and nothing a later
step already owns (PostgreSQL is ``postgres.package``).  Like every mutating
step it is consent-gated, idempotent (what is on PATH is never reinstalled) and
reports what it installed as a resource, so uninstall can say AQ put it there.
It never types a password: if ``sudo`` would prompt, the step is ``needs_user``
with the one command to run.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Sequence

from .command import INSTALL_TIMEOUT, CommandOutput, CommandRunner, run_command
from .prerequisites import STEP_HOST
from .results import ResourceRecord, StepResult
from .steps import StepContext, StepSpec

OWNER = "wsl"

STEP_PACKAGES = "wsl.packages"

#: ``(apt package, the command it provides)``, in the order apt is given them.
#: Git is listed as well as tmux: the bootstrap script installs it before ``aq``
#: exists, but a contributor who installed AQ another way still reaches
#: ``prereq.git``.
APT_PREREQUISITES: tuple[tuple[str, str], ...] = (("git", "git"), ("tmux", "tmux"))

#: apt's own lock waits can be slow on a cold WSL distribution.
APT_TIMEOUT = max(INSTALL_TIMEOUT, 1800.0)


def packages_step(
    *,
    which: Callable[[str], str | None] | None = None,
    runner: CommandRunner | None = None,
    packages: Sequence[tuple[str, str]] = APT_PREREQUISITES,
    timeout: float = APT_TIMEOUT,
) -> StepSpec:
    """Install the missing apt prerequisites this host's later checks require."""
    lookup = which or shutil.which
    execute = runner or run_command
    required = tuple(packages)

    def _missing() -> tuple[tuple[str, str], ...]:
        return tuple((package, command) for package, command in required if not lookup(command))

    def _sudo_ready() -> bool:
        return execute(["sudo", "-n", "true"], timeout=10.0).ok

    def run(context: StepContext) -> StepResult:
        missing = _missing()
        names = [package for package, _ in required]
        if not missing:
            return StepResult.succeeded(
                STEP_PACKAGES,
                f"already installed: {', '.join(names)}",
                detail={"required": names, "installed": []},
            )

        wanted = [package for package, _ in missing]
        sudo = [] if os.geteuid() == 0 else ["sudo"]
        if sudo and not _sudo_ready():
            return StepResult.needs_user(
                STEP_PACKAGES,
                f"installing {', '.join(wanted)} needs root and sudo would prompt for a password",
                (
                    "Run `sudo -v` in this terminal and rerun `aq install`, or install them "
                    f"yourself with `sudo apt-get install -y {' '.join(wanted)}`."
                ),
                detail={"missing": wanted},
            )

        # `apt-get install` on a distribution whose package lists were never
        # refreshed fails with "Unable to locate package"; refreshing is part of
        # installing, not a separate concern the operator should have to know.
        refresh: CommandOutput = execute([*sudo, "apt-get", "update"], timeout=timeout)
        install: CommandOutput = execute(
            [*sudo, "apt-get", "install", "-y", *wanted], timeout=timeout
        )
        if not install.ok:
            return StepResult.failed(
                STEP_PACKAGES,
                f"`apt-get install -y {' '.join(wanted)}` failed: {install.message()}",
                (
                    f"Run `sudo apt-get install -y {' '.join(wanted)}` yourself to see apt's "
                    "own output, resolve what it reports (another apt process holding the "
                    "lock is the usual cause), then rerun `aq install`."
                ),
                detail={
                    "missing": wanted,
                    "exit_code": install.returncode,
                    "update_ok": refresh.ok,
                },
            )

        still_missing = [command for _, command in missing if not lookup(command)]
        if still_missing:
            return StepResult.failed(
                STEP_PACKAGES,
                f"apt reported success but {', '.join(still_missing)} is still not on PATH",
                (
                    "Check what apt installed with `dpkg -L "
                    f"{' '.join(wanted)} | grep bin/`, then rerun `aq install`."
                ),
                detail={"missing": wanted, "not_on_path": still_missing},
            )

        return StepResult.succeeded(
            STEP_PACKAGES,
            f"installed with apt: {', '.join(wanted)}",
            detail={"required": names, "installed": wanted},
            resources=tuple(
                ResourceRecord(
                    kind="apt-package",
                    id=package,
                    owned=True,
                    reused=False,
                    detail={"command": command},
                )
                for package, command in missing
            ),
        )

    return StepSpec(
        id=STEP_PACKAGES,
        title="Install prerequisites with apt",
        description=(
            "Installs the missing prerequisites ("
            + ", ".join(package for package, _ in required)
            + ") with `apt-get install`. Anything already on PATH is left alone."
        ),
        run=run,
        depends_on=(STEP_HOST,),
        mutating=True,
        consent_prompt="Install the missing prerequisites with apt?",
        verify=lambda context: not _missing(),
        owner=OWNER,
    )


def wsl_steps(
    *,
    which: Callable[[str], str | None] | None = None,
    runner: CommandRunner | None = None,
    **_ignored: object,
) -> tuple[StepSpec, ...]:
    """The WSL adapter's steps, in dependency order."""
    return (packages_step(which=which, runner=runner),)


__all__ = [
    "APT_PREREQUISITES",
    "APT_TIMEOUT",
    "OWNER",
    "STEP_PACKAGES",
    "packages_step",
    "wsl_steps",
]
