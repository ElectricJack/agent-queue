"""Composing the engine's steps with the platform adapter for this host.

``aq install`` runs one registry.  Which steps are in it depends on where it is
running: the engine's admission and prerequisite checks are true everywhere,
and everything that knows how to *change* a host belongs to that host's adapter
(``noble-apex.4`` for macOS; the WSL adapter registers here beside it).

Two things make the composition worth its own module rather than a branch
inside the CLI:

* **Order.**  A platform adapter installs the very prerequisites the engine
  then checks, so ``prereq.git``/``prereq.tmux`` must run *after* the adapter's
  provisioning step — expressed as a real dependency, not as a registration
  accident.
* **Lookup.**  A formula Homebrew just installed is in ``<prefix>/bin``, which
  is on the next shell's ``PATH`` and not on this process's, so the macOS host
  hands every command check a prefix-aware ``which``.

An unsupported host still composes: the engine refuses it before any step runs,
and ``--list-steps`` on a machine that is not a supported host prints the
engine's own steps rather than nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .macos import (
    DEFAULT_PREFIXES,
    STEP_PACKAGES,
    STEP_PYTHON_RUNTIME,
    brew_aware_which,
    macos_steps,
)
from .platform import (
    HOST_MACOS_ARM,
    HOST_MACOS_INTEL,
    SupportVerdict,
    describe_host,
)
from .prerequisites import (
    STEP_HOST,
    data_directory_step,
    git_step,
    host_step,
    python_step,
    tmux_step,
)
from .steps import StepRegistry, StepSpec

#: Host paths this build has a platform adapter for.
MACOS_HOSTS = frozenset({HOST_MACOS_ARM, HOST_MACOS_INTEL})


def platform_steps(
    support: SupportVerdict,
    **kwargs: Any,
) -> tuple[StepSpec, ...]:
    """The adapter steps for *support*'s host path, or ``()`` when there is none."""
    if support.host_path in MACOS_HOSTS:
        return macos_steps(**kwargs)
    return ()


def build_registry(
    support: SupportVerdict | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    state_dir: Path | None = None,
    prefixes: Mapping[str, Path] | None = None,
    **adapter_kwargs: Any,
) -> StepRegistry:
    """Build the full step registry for the host described by *support*."""
    verdict = support or describe_host(environ=environ)
    macos = verdict.host_path in MACOS_HOSTS
    # The prefix-aware lookup has to know the *same* prefixes the adapter uses,
    # or the engine's command checks would miss what Homebrew just installed.
    lookup = (
        brew_aware_which(which, list(dict(prefixes or DEFAULT_PREFIXES).values()))
        if macos
        else which
    )

    registry = StepRegistry((host_step(),))
    registry.extend(
        platform_steps(
            verdict,
            environ=environ,
            which=lookup,
            **({"prefixes": prefixes} if macos else {}),
            **adapter_kwargs,
        )
    )

    # On macOS the platform adapter has already said something specific about
    # the interpreter and has installed the prerequisites, so the engine's
    # generic checks run after it and revalidate its work.
    python_after = (STEP_HOST, STEP_PYTHON_RUNTIME) if macos else (STEP_HOST,)
    command_after = (STEP_HOST, STEP_PACKAGES) if macos else (STEP_HOST,)
    registry.extend(
        (
            python_step(depends_on=python_after),
            git_step(which=lookup, depends_on=command_after),
            tmux_step(which=lookup, depends_on=command_after),
            data_directory_step(environ=environ, path=state_dir),
        )
    )
    return registry


__all__ = ["MACOS_HOSTS", "build_registry", "platform_steps"]
