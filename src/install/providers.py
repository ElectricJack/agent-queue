"""Optional agent-CLI provider adapters for :mod:`src.install`.

Provider credentials belong to their own CLIs.  These adapters only detect an
executable, install a selected CLI using the provider's documented installer,
and record non-secret executable metadata in AQ's resume record.  Login and
authentication readiness are deliberately owned by the follow-up login flow.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .prerequisites import STEP_TMUX
from .results import ResourceRecord, StepResult
from .steps import StepContext, StepSpec

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
CommandLookup = Callable[[str], str | None]
_VERSION_PATTERN = re.compile(r"\b[vV]?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9._-]+)?\b")


#: The directories a provider's own installer writes into, relative to ``$HOME``.
#: ``claude.ai/install.sh`` and ``chatgpt.com/codex/install.sh`` both link their
#: executable into ``~/.local/bin``, and npm's user prefix is the same place.
USER_BIN_DIRS: tuple[str, ...] = (".local/bin", "bin")


def user_bin_aware_which(
    base: CommandLookup | None = None,
    home: Path | None = None,
) -> CommandLookup:
    """``which`` that also looks where a provider's installer puts its binary.

    ``~/.local/bin`` is on the *next* login shell's PATH -- the bootstrap appends
    it to the profile -- but not necessarily on this process's, so the install
    step installed Claude Code into it and then reported "claude was not
    executable on PATH after installation", stopping a run whose work had
    actually succeeded.  Looking in the directory the installer just wrote to is
    the same fix ``brew_aware_which`` makes for a Homebrew prefix.
    """
    lookup = base or shutil.which
    root = home or Path.home()

    def which(command: str) -> str | None:
        found = lookup(command)
        if found:
            return found
        for folder in USER_BIN_DIRS:
            candidate = root / folder / command
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    return which


@dataclass(frozen=True, slots=True)
class ProviderInstaller:
    """The executable and supported installer command for one harness provider."""

    id: str
    title: str
    executable: str
    install_command: tuple[str, ...]
    install_hint: str

    @property
    def capability(self) -> str:
        return f"provider.{self.id}"

    @property
    def step_id(self) -> str:
        return f"provider.{self.id}-cli"


@dataclass(frozen=True, slots=True)
class ExecutableProbe:
    """The non-secret observation persisted for a provider executable."""

    path: str | None
    version: str | None = None

    @property
    def available(self) -> bool:
        return self.path is not None

    def detail(self) -> dict[str, str | None]:
        return {"executable": self.path, "version": self.version}


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=False, capture_output=True, text=True)


#: How much of a provider installer's own output a failure carries.
EXCERPT_LINES = 8


def _excerpt(completed: subprocess.CompletedProcess[str]) -> str:
    """The installer's own last lines, for a failure that cannot explain itself.

    Provider installers are third-party scripts; when one of them ends in a state
    AQ did not expect, what it printed is the evidence.  stderr first, because a
    script that fails usually says so there.
    """
    for stream in (completed.stderr, completed.stdout):
        lines = [line.rstrip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            tail = lines[-EXCERPT_LINES:]
            return "; installer output: " + " | ".join(tail)
    return ""


def probe_executable(
    installer: ProviderInstaller,
    *,
    which: CommandLookup = shutil.which,
    runner: CommandRunner = _run,
) -> ExecutableProbe:
    """Find a provider executable and obtain its version without reading credentials."""
    path = which(installer.executable)
    if not path:
        return ExecutableProbe(path=None)
    try:
        # The *resolved* path, not the bare name: a CLI its own installer put in
        # ~/.local/bin is findable (see user_bin_aware_which) but not runnable by
        # name until a new login shell picks up PATH, and running the name
        # instead made every such install report "not runnable" right after it
        # had succeeded.
        result = runner((path, "--version"))
    except OSError:
        # A stale PATH entry is not an available harness.  The install step
        # will offer the documented repair path instead of reporting success.
        return ExecutableProbe(path=None)
    if result.returncode != 0:
        return ExecutableProbe(path=None)
    output = result.stdout or result.stderr or ""
    # Keep only a conventional version token.  Even a broken or hostile PATH
    # executable cannot inject its arbitrary output into the resume record or
    # the machine-readable installer result.
    match = _VERSION_PATTERN.search(output)
    version = match.group(0) if match else "unknown"
    return ExecutableProbe(path=path, version=version)


def provider_step(
    installer: ProviderInstaller,
    *,
    which: CommandLookup = shutil.which,
    runner: CommandRunner = _run,
) -> StepSpec:
    """Create the optional, idempotent installation step for *installer*."""

    def observe() -> ExecutableProbe:
        return probe_executable(installer, which=which, runner=runner)

    def resource(probe: ExecutableProbe, *, owned: bool, reused: bool) -> ResourceRecord:
        return ResourceRecord(
            kind="provider-cli",
            id=installer.id,
            owned=owned,
            reused=reused,
            detail=probe.detail(),
        )

    def run(context: StepContext) -> StepResult:
        del context  # Credentials and interactive login are intentionally out of scope here.
        before = observe()
        if before.available:
            return StepResult.succeeded(
                installer.step_id,
                f"reusing {installer.executable} at {before.path} ({before.version})",
                detail=before.detail(),
                resources=(resource(before, owned=False, reused=True),),
            )

        try:
            completed = runner(installer.install_command)
        except OSError as error:
            return StepResult.failed(
                installer.step_id,
                f"could not start the {installer.title} installer ({type(error).__name__})",
                installer.install_hint,
            )
        if completed.returncode != 0:
            return StepResult.failed(
                installer.step_id,
                f"the {installer.title} installer exited with status {completed.returncode}"
                + _excerpt(completed),
                installer.install_hint,
            )

        after = observe()
        if not after.available:
            # The installer said it succeeded and left nothing runnable behind.
            # Its own last words are the only evidence of why, and dropping them
            # left a failure whose single hint ("open a new shell") was a guess:
            # a half-written download and a PATH that has not caught up look
            # identical from here.
            return StepResult.failed(
                installer.step_id,
                f"the {installer.title} installer reported success but "
                f"{installer.executable} is not runnable" + _excerpt(completed),
                f"Run `{' '.join(installer.install_command)}` yourself to see the installer's "
                f"own output, then rerun `aq install --with {installer.capability}`. "
                f"{installer.install_hint}",
                detail={"install_command": list(installer.install_command)},
            )
        return StepResult.succeeded(
            installer.step_id,
            f"installed {installer.executable} at {after.path} ({after.version})",
            detail=after.detail(),
            resources=(resource(after, owned=True, reused=False),),
        )

    return StepSpec(
        id=installer.step_id,
        title=f"Install or reuse {installer.title}",
        description=(
            f"Detects {installer.executable} on PATH and verifies `--version`; when selected, "
            "runs the provider's documented installer without handling credentials."
        ),
        run=run,
        depends_on=("host.supported", STEP_TMUX),
        capability=installer.capability,
        mutating=True,
        consent_prompt=f"Install {installer.title} with its official installer if it is missing?",
        verify=lambda context: observe().available,
        owner="provider",
    )


CLAUDE_CODE = ProviderInstaller(
    id="claude",
    title="Claude Code",
    executable="claude",
    install_command=("bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"),
    install_hint="See https://code.claude.com/docs/en/getting-started and rerun the installer.",
)

CODEX = ProviderInstaller(
    id="codex",
    title="Codex CLI",
    executable="codex",
    install_command=("sh", "-c", "curl -fsSL https://chatgpt.com/codex/install.sh | sh"),
    install_hint="See https://learn.chatgpt.com/docs/codex/cli and rerun the installer.",
)

GEMINI = ProviderInstaller(
    id="gemini",
    title="Gemini CLI",
    executable="gemini",
    install_command=("npm", "install", "-g", "@google/gemini-cli"),
    install_hint="Install Node.js/npm, then see https://geminicli.com/docs/get-started/installation/.",
)


def provider_installers(
    additional: Iterable[ProviderInstaller] = (),
) -> tuple[ProviderInstaller, ...]:
    """Return AQ's supported providers plus caller-supplied provider adapters.

    ``additional`` makes the registry extensible without teaching the engine
    about individual providers.  Duplicate ids are rejected before an
    ambiguous capability or step can reach the installer.
    """
    installers = (CLAUDE_CODE, CODEX, GEMINI, *additional)
    ids = [installer.id for installer in installers]
    if len(ids) != len(set(ids)):
        raise ValueError("provider installer ids must be unique")
    return installers


def provider_steps(
    *,
    which: CommandLookup = shutil.which,
    runner: CommandRunner = _run,
    additional: Iterable[ProviderInstaller] = (),
) -> tuple[StepSpec, ...]:
    """Build all registered provider steps in stable provider order."""
    return tuple(
        provider_step(installer, which=which, runner=runner)
        for installer in provider_installers(additional)
    )


__all__ = [
    "CLAUDE_CODE",
    "EXCERPT_LINES",
    "CODEX",
    "ExecutableProbe",
    "GEMINI",
    "ProviderInstaller",
    "USER_BIN_DIRS",
    "probe_executable",
    "provider_installers",
    "provider_step",
    "provider_steps",
    "user_bin_aware_which",
]
