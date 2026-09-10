"""Optional agent-CLI provider adapters for :mod:`src.install`.

Provider credentials belong to their own CLIs.  These adapters only detect an
executable, install a selected CLI using the provider's documented installer,
and record non-secret executable metadata in AQ's resume record.  Login and
authentication readiness are deliberately owned by the follow-up login flow.
"""

from __future__ import annotations

import shutil
import subprocess
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from .prerequisites import STEP_TMUX
from .results import ResourceRecord, StepResult
from .steps import StepContext, StepSpec


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
CommandLookup = Callable[[str], str | None]
_VERSION_PATTERN = re.compile(r"\b[vV]?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9._-]+)?\b")


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
        result = runner((installer.executable, "--version"))
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
                f"the {installer.title} installer exited with status {completed.returncode}",
                installer.install_hint,
            )

        after = observe()
        if not after.available:
            return StepResult.failed(
                installer.step_id,
                f"{installer.executable} was not executable on PATH after installation",
                f"Open a new shell so PATH updates take effect, then rerun `aq install --with "
                f"{installer.capability}`. {installer.install_hint}",
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
    "CODEX",
    "GEMINI",
    "ExecutableProbe",
    "ProviderInstaller",
    "probe_executable",
    "provider_installers",
    "provider_step",
    "provider_steps",
]
