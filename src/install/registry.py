"""Composing the engine's steps with the platform adapter for this host.

``aq install`` runs one registry.  Which steps are in it depends on where it is
running: the engine's admission and prerequisite checks are true everywhere,
and everything that knows how to *change* a host belongs to that host's
adapter.

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

import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from .logins import login_steps
from .macos import (
    DEFAULT_PREFIXES,
    STEP_PACKAGES,
    STEP_PYTHON_RUNTIME,
    brew_aware_which,
    macos_steps,
)
from .onboarding import onboarding_steps as default_onboarding_steps
from .platform import (
    HOST_MACOS_ARM,
    HOST_MACOS_INTEL,
    SupportVerdict,
    describe_host,
)
from .postgres_steps import STEP_CONNECTION as STEP_POSTGRES_CONNECTION
from .postgres_steps import STEP_PACKAGE as STEP_POSTGRES_PACKAGE
from .postgres_steps import postgres_steps
from .prerequisites import (
    STEP_DATA_DIR,
    STEP_HOST,
    data_directory_step,
    git_step,
    host_step,
    python_step,
    tmux_step,
)
from .providers import provider_installers, provider_steps
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
    database_steps: Iterable[StepSpec] | None = None,
    onboarding_steps: Iterable[StepSpec] | None = None,
    **adapter_kwargs: Any,
) -> StepRegistry:
    """Build the full step registry for the host described by *support*.

    ``database_steps`` and ``onboarding_steps`` replace those groups, which is
    what lets a suite about one adapter compose the registry without dragging
    in the others: a macOS test has no PostgreSQL and therefore no
    configuration a daemon could load.
    """
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
    # PostgreSQL's macOS package plan uses Homebrew.  Make that dependency
    # explicit so a selected managed database cannot race the macOS bootstrap.
    postgres_adapter_steps = (
        tuple(database_steps)
        if database_steps is not None
        else postgres_steps(
            environ=environ,
            which=lookup or shutil.which,
            state_dir=state_dir,
        )
    )
    if macos:
        postgres_adapter_steps = tuple(
            replace(step, depends_on=(STEP_HOST, STEP_PACKAGES))
            if step.id == STEP_POSTGRES_PACKAGE
            else step
            for step in postgres_adapter_steps
        )
    registry.extend(postgres_adapter_steps)
    # Provider adapters remain part of every CLI registry.  They are
    # capability-gated, so this preserves the normal install's behavior while
    # allowing a platform adapter to order the prerequisites they rely on.
    installers = provider_installers()
    registry.extend(provider_steps(which=lookup))
    registry.extend(login_steps(environ=environ, which=lookup or shutil.which, installers=installers))
    # The onboarding steps close the newcomer's path: a configuration with
    # defaults for this box, a daemon that answers and the dashboard URL.  The
    # daemon needs a database and a configuration that names it, so when the
    # PostgreSQL adapter is present the dependency is stated rather than left
    # to registration order — the engine's topological sort interleaves
    # independent branches, and a daemon started before the credential was
    # written would fail for a reason nobody could read.
    onboarding_after = (
        (STEP_POSTGRES_CONNECTION,)
        if STEP_POSTGRES_CONNECTION in registry
        else (STEP_DATA_DIR,)
    )
    registry.extend(
        tuple(onboarding_steps)
        if onboarding_steps is not None
        else default_onboarding_steps(
            environ=environ,
            home=state_dir,
            which=lookup or shutil.which,
            depends_on=onboarding_after,
        )
    )
    return registry


__all__ = ["MACOS_HOSTS", "build_registry", "platform_steps"]
