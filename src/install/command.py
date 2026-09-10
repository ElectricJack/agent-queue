"""Running an external command from an installer step.

Platform adapters have to ask the host questions no Python API answers —
"which Homebrew prefix is this?", "is this shell translated by Rosetta?" — and
occasionally to install a package.  They all do it through one injectable
callable so the whole macOS/WSL matrix stays provable on a single machine:
``CommandRunner`` is a parameter of every step factory, and :func:`run_command`
is only its default.

The runner is deliberately synchronous.  ``aq install`` runs before a daemon,
an event loop or a database exists, and :class:`~src.install.steps.StepSpec`
declares a synchronous ``run``; the project's async-first rule is about the
daemon's own I/O, not about a pre-daemon CLI that shells out to ``brew``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

#: How long a probe (``xcode-select -p``, ``brew --prefix``) may take before it
#: is reported as a failure rather than hanging the installer.
DEFAULT_TIMEOUT = 30.0

#: Installing a package is slow; a compile from source is slower still.
INSTALL_TIMEOUT = 1800.0


@dataclass(frozen=True, slots=True)
class CommandOutput:
    """What one external command did.

    ``returncode`` is ``None`` for a command that never ran (missing
    executable) or never finished (timeout); ``error`` then carries the reason,
    so a step can report *why* a probe produced nothing instead of guessing.
    """

    argv: tuple[str, ...]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        """The trimmed stdout — what a probe actually wanted."""
        return self.stdout.strip()

    def message(self, *, limit: int = 300) -> str:
        """A short, single-line diagnostic for a summary or remediation.

        Long build logs are the normal failure output of a package manager and
        are useless in a one-line step summary, so the tail is what survives:
        the last non-empty line, truncated.
        """
        if self.error:
            return self.error
        for stream in (self.stderr, self.stdout):
            lines = [line.strip() for line in stream.splitlines() if line.strip()]
            if lines:
                tail = lines[-1]
                return tail if len(tail) <= limit else tail[: limit - 1] + "…"
        return f"exit status {self.returncode}"


CommandRunner = Callable[..., CommandOutput]


def run_command(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> CommandOutput:
    """Run *argv* and capture it.  Never raises; never inherits a terminal.

    ``stdin`` is closed (or fed *input_text*) on purpose: a step that would
    otherwise sit at an interactive password prompt must fail with something a
    human can read, and an unattended run must never block on a terminal it
    does not have.
    """
    command = tuple(str(part) for part in argv)
    if not command:
        return CommandOutput(argv=(), error="no command given")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            input=input_text if input_text is not None else "",
            check=False,
        )
    except FileNotFoundError:
        return CommandOutput(argv=command, error=f"{command[0]} is not installed")
    except PermissionError as error:
        return CommandOutput(argv=command, error=f"{command[0]} is not executable: {error}")
    except subprocess.TimeoutExpired:
        return CommandOutput(argv=command, error=f"{command[0]} did not finish within {timeout:g}s")
    except OSError as error:  # pragma: no cover - defensive; exec failures are rare
        return CommandOutput(argv=command, error=f"{command[0]} could not be run: {error}")
    return CommandOutput(
        argv=command,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "INSTALL_TIMEOUT",
    "CommandOutput",
    "CommandRunner",
    "run_command",
]
