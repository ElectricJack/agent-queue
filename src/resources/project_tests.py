"""Which interpreter and preconditions ``aq test`` uses for the tests it runs.

``aq test`` began as agent-queue's own wrapper: it ran ``sys.executable -m
pytest`` and refused to start without ``POSTGRES_TEST_DSN``.  In another
project's worktree that meant agent-queue's interpreter and a refusal for a
DSN those tests never read, so quilt-trader's workers ran their venv's pytest
by hand, outside the box-wide test slots (2026-09-28).  The slots are box
policy and apply to every project; the interpreter and the preconditions
belong to the project whose tests are being run.

The interpreter, first match wins:

1. ``resources.test_interpreters[<project id>]`` in the operator config.  The
   project id is ``aq test --aq-project``, else the session's
   ``AQ_PROJECT_ID``.  A configured interpreter that does not exist is an
   error, never a silent fallback.
2. agent-queue itself: the interpreter running ``aq``.
3. ``.venv/bin/python`` in the project root, the repository root or, from a
   linked worktree, the main checkout: worker slots carry no venv of their
   own.
4. The interpreter running ``aq``, which the caller reports.

agent-queue is recognised by content, not by project id: the nearest
``pyproject.toml`` names the ``agent-queue`` distribution.  That holds in a
worker slot, in CI and in the development publisher's retained clone alike,
and it is the only project that requires ``POSTGRES_TEST_DSN``.

Everything here reads files; nothing starts a process except
:func:`has_xdist` probing a foreign interpreter.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: The distribution name in agent-queue's own ``pyproject.toml``.
AGENT_QUEUE_DISTRIBUTION = "agent-queue"

#: Where a project's virtual environment keeps its interpreter.
_VENV_PYTHON = Path(".venv") / "bin" / "python"

#: Seconds a foreign interpreter gets to answer "is pytest-xdist installed".
_PROBE_TIMEOUT = 30.0

#: How the interpreter was chosen.
CONFIG = "config"
AGENT_QUEUE = "agent-queue"
VENV = "venv"
FALLBACK = "fallback"


class ProjectTestsError(Exception):
    """The project's tests cannot be started as configured."""


@dataclass(frozen=True)
class ProjectTestSetup:
    """How to run one project's tests."""

    #: The project's root: the nearest ``pyproject.toml``, else the repository.
    root: Path
    #: Interpreter that runs ``-m pytest``.
    python: str
    #: :data:`CONFIG`, :data:`AGENT_QUEUE`, :data:`VENV` or :data:`FALLBACK`.
    source: str
    #: agent-queue's own suite: ``POSTGRES_TEST_DSN`` is required.
    requires_postgres: bool

    @property
    def foreign(self) -> bool:
        """True when pytest runs under an interpreter other than ``aq``'s."""
        return not _same_interpreter(self.python, sys.executable)

    def child_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """*env* with a foreign virtual environment activated.

        A test that shells out to ``python`` must find the project's
        interpreter, not whichever venv the session's ``PATH`` names first.
        """
        result = dict(env)
        venv = _venv_of(self.python)
        if not self.foreign or venv is None:
            return result
        result["VIRTUAL_ENV"] = str(venv)
        result["PATH"] = os.pathsep.join(
            part for part in (str(venv / "bin"), env.get("PATH", "")) if part
        )
        result.pop("PYTHONHOME", None)
        return result


def resolve(
    anchor: Path,
    *,
    project_id: str | None = None,
    interpreters: Mapping[str, str] | None = None,
) -> ProjectTestSetup:
    """The interpreter and preconditions for tests at or below *anchor*.

    Raises :class:`ProjectTestsError` when the operator configured an
    interpreter for *project_id* that does not exist.
    """
    anchor = anchor.resolve()
    repo = repository_root(anchor)
    pyproject = _nearest_pyproject(anchor, stop=repo)
    root = pyproject.parent if pyproject else (repo or anchor)
    requires_postgres = pyproject is not None and (
        distribution_name(pyproject) == AGENT_QUEUE_DISTRIBUTION
    )
    homes = _unique([root, repo, main_checkout(repo) if repo else None])

    configured = (interpreters or {}).get(project_id or "")
    if configured:
        python = _configured_python(configured, homes)
        if python is None:
            raise ProjectTestsError(
                f"resources.test_interpreters[{project_id!r}] names {configured}, "
                "which does not exist"
            )
        return ProjectTestSetup(root, python, CONFIG, requires_postgres)
    if requires_postgres:
        return ProjectTestSetup(root, sys.executable, AGENT_QUEUE, True)
    for home in homes:
        candidate = home / _VENV_PYTHON
        if _is_executable(candidate):
            return ProjectTestSetup(root, str(candidate), VENV, False)
    return ProjectTestSetup(root, sys.executable, FALLBACK, False)


def repository_root(start: Path) -> Path | None:
    """The nearest directory at or above *start* holding a ``.git`` entry."""
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def main_checkout(repo: Path) -> Path | None:
    """The main working tree when *repo* is a linked worktree, else ``None``.

    Read from the files ``git worktree add`` writes -- ``.git`` holds
    ``gitdir: <admin dir>`` and the admin dir's ``commondir`` points back at
    the shared ``.git`` -- so no ``git`` process is needed.  A submodule's
    ``.git`` file has no ``commondir`` and is not a linked worktree.
    """
    marker = repo / ".git"
    if not marker.is_file():
        return None
    try:
        line = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    admin = Path(line.removeprefix("gitdir:").strip())
    if not admin.is_absolute():
        admin = repo / admin
    try:
        common = Path(admin / (admin / "commondir").read_text(encoding="utf-8").strip())
    except OSError:
        return None
    common = common.resolve()
    if common.name != ".git":
        return None
    return common.parent if common.parent != repo else None


def distribution_name(pyproject: Path) -> str | None:
    """``[project].name`` from *pyproject*, or ``None`` when unreadable."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    project = data.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    return name if isinstance(name, str) else None


def has_xdist(python: str) -> bool:
    """True when *python* can import pytest-xdist.

    ``aq``'s own interpreter answers in process.  A foreign one is asked, and
    any failure to answer counts as "no": a serial run is slower, an ``-n``
    that pytest does not recognise runs nothing at all.
    """
    if _same_interpreter(python, sys.executable):
        return importlib.util.find_spec("xdist") is not None
    probe = "import importlib.util, sys; sys.exit(importlib.util.find_spec('xdist') is None)"
    try:
        completed = subprocess.run(
            [python, "-c", probe],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _nearest_pyproject(start: Path, *, stop: Path | None) -> Path | None:
    """The nearest ``pyproject.toml`` at or above *start*, not above *stop*."""
    for directory in (start, *start.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
        if directory == stop:
            return None
    return None


def _configured_python(configured: str, homes: list[Path]) -> str | None:
    """*configured* as an existing interpreter path.

    ``~`` is expanded; a relative path is tried against each of *homes*, so
    ``.venv/bin/python`` finds the main checkout's venv from a worker slot.
    """
    path = Path(configured).expanduser()
    candidates = [path] if path.is_absolute() else [home / path for home in homes]
    for candidate in candidates:
        if _is_executable(candidate):
            return str(candidate)
    return None


def _venv_of(python: str) -> Path | None:
    """The virtual environment *python* belongs to, when it is a venv's."""
    venv = Path(python).parent.parent
    return venv if (venv / "pyvenv.cfg").is_file() else None


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _same_interpreter(left: str, right: str) -> bool:
    """Whether two interpreter paths name the same interpreter.

    Compared as absolute paths, never resolved: a venv's ``python`` is a
    symlink to the base interpreter, so resolving would call every venv on
    the box the same one.
    """
    return os.path.abspath(left) == os.path.abspath(right)


def _unique(paths: list[Path | None]) -> list[Path]:
    seen: list[Path] = []
    for path in paths:
        if path is not None and path not in seen:
            seen.append(path)
    return seen
