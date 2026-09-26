"""Static impact ``S``: the test modules a change reaches through imports.

Spec §5.1 adopts ``pytest-impacted`` behind the :class:`StaticImpact`
protocol instead of writing another graph engine, and makes that adoption
depend on fixture evidence (``tests/test_selection_static_impact.py``).
:class:`PytestImpactedAdapter` drives the library's standalone
``impacted-tests`` command, the one interface it documents for listing
impacted test files without running pytest, and never imports the library.
Only the pinned version (:data:`PINNED_ENGINE`) is used unless a caller names
an executable.

The engine's two git modes each see part of a snapshot: ``branch`` diffs two
commits and ``unstaged`` sees the index, the working tree and untracked
files. The adapter runs both, branch mode against the snapshot's merge-base,
and unions the modules they name.

The fixture evaluation found changes the engine does not follow, because its
graph has only the explicit import edges of the current tree:

- a deleted or renamed-away module loses every old importer;
- an edit to a package's ``__init__.py`` reaches no importer of its
  submodules, although importing any of them runs it;
- a change to a module that an ``__init__.py`` or a ``conftest.py`` loads
  (:mod:`src.test_selection.load_closure`) reaches only that module's direct
  importers, although it runs for every submodule or test beneath;
- a module in a directory without ``__init__.py`` below the analysed package
  is never discovered.

For any of these the adapter answers with the whole catalogue, complete, and
names the change in :attr:`StaticResult.widened_by`: static impact of a hub
module is broad, and spec §4.3 leaves narrowing it to a promoted Jev policy.
A package or test directory that is entirely new has no old importer, so
adding one is followed. A missing, unpinned, failed, timed-out or unreadable
engine, and an incomplete snapshot, make the result incomplete instead; the
caller then falls back to the whole universe with no narrowing at all.

Python outside the analysed directories (``scripts/``, ``migrations/``,
generated clients), data files, registries and source-scanning tests are
invisible to ``S``: they belong to the rule map, as no static graph proves
their absence.
"""

from __future__ import annotations

import asyncio
import errno
import importlib.metadata
import json
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

from src.env_scrub import scrub_env
from src.test_selection.load_closure import LOAD_ROOTS

if TYPE_CHECKING:
    from src.test_selection.catalogue import Catalogue
    from src.test_selection.snapshot import ChangeSnapshot

#: Codes for :attr:`StaticResult.reason`; ``static_unavailable`` is also
#: ``reasons.STATIC_UNAVAILABLE``.
STATIC_UNAVAILABLE = "static_unavailable"
STATIC_TIMEOUT = "static_timeout"
STATIC_ERROR = "static_error"
STATIC_REASONS = (STATIC_UNAVAILABLE, STATIC_TIMEOUT, STATIC_ERROR)

ENGINE_DISTRIBUTION = "pytest-impacted"
ENGINE_EXECUTABLE = "impacted-tests"
ENGINE_UNAVAILABLE = "unavailable"
#: The version the fixture evaluation covers; ``pyproject.toml`` pins the same.
PINNED_ENGINE = "pytest-impacted 0.30.0"
#: The label of an engine a caller named: its version is not checked.
UNVERIFIED_ENGINE = "pytest-impacted (unverified)"

_LOAD_CLOSURE_SCRIPT = Path(__file__).with_name("load_closure.py")


@dataclass(frozen=True)
class StaticResult:
    modules: frozenset[str]  # catalogued test modules, posix relative to the workspace
    complete: bool
    engine: str  # PINNED_ENGINE | UNVERIFIED_ENGINE | "fixed" | "unavailable"
    reason: str | None  # one of STATIC_REASONS; None when complete
    unknown_outputs: int  # distinct paths the engine named that are no catalogued module
    elapsed_ms: int
    # Why it is incomplete: a run and its failure ("branch:exit_1",
    # "unstaged:undecodable", "load_closure"), "not_installed",
    # "version:<v>" or "snapshot_incomplete". Never engine output.
    detail: str | None = None
    # Why a complete result is the whole catalogue: "<kind>:<path>", kind one
    # of deleted, renamed, package_init, import_time, unresolvable.
    widened_by: str | None = None


class StaticImpact(Protocol):
    async def impacted(self, snapshot: ChangeSnapshot, *, catalogue: Catalogue) -> StaticResult: ...


def engine_version() -> str:
    """``"pytest-impacted <version>"`` as installed with this interpreter, or ``"unavailable"``."""
    try:
        return f"{ENGINE_DISTRIBUTION} {importlib.metadata.version(ENGINE_DISTRIBUTION)}"
    except importlib.metadata.PackageNotFoundError:
        return ENGINE_UNAVAILABLE


def default_executable() -> str | None:
    """The pinned engine's ``impacted-tests``: beside this interpreter, else on ``PATH``.

    ``None`` unless :data:`PINNED_ENGINE` is what this interpreter has
    installed. A daemon started as ``.venv/bin/agent-queue`` need not have
    ``.venv/bin`` on its ``PATH``, but the ``test-selection`` extra installs
    the command next to the interpreter.
    """
    if engine_version() != PINNED_ENGINE:
        return None
    if sys.executable:
        beside = shutil.which(ENGINE_EXECUTABLE, path=os.path.dirname(sys.executable))
        if beside:
            return beside
    return shutil.which(ENGINE_EXECUTABLE)


class UnavailableStaticImpact:
    """No static engine: never complete, so the caller falls back to the universe."""

    async def impacted(self, snapshot: ChangeSnapshot, *, catalogue: Catalogue) -> StaticResult:
        return StaticResult(frozenset(), False, ENGINE_UNAVAILABLE, STATIC_UNAVAILABLE, 0, 0)


class FixedStaticImpact:
    """A preset answer for tests, held to the catalogue like a real engine's."""

    def __init__(
        self, modules: Iterable[str], *, complete: bool = True, reason: str | None = None
    ) -> None:
        self._modules = frozenset(modules)
        self._complete = complete
        self._reason = None if complete else (reason or STATIC_ERROR)

    async def impacted(self, snapshot: ChangeSnapshot, *, catalogue: Catalogue) -> StaticResult:
        universe = catalogue.universe
        return StaticResult(
            modules=self._modules & universe,
            complete=self._complete,
            engine="fixed",
            reason=self._reason,
            unknown_outputs=len(self._modules - universe),
            elapsed_ms=0,
        )


@dataclass(frozen=True)
class _Run:
    """One subprocess: its stdout, or why there is none."""

    stdout: bytes = b""
    reason: str | None = None
    detail: str | None = None


class PytestImpactedAdapter:
    """``S`` from the ``impacted-tests`` command of ``pytest-impacted``.

    *module* and *tests_dir* are the engine's ``--module`` and ``--tests-dir``:
    directories (or dotted names) below the workspace root. *timeout_seconds*
    is the deadline for the whole call; the engine runs and the load closure
    share it. *executable* is the path of ``impacted-tests`` and skips the
    version pin; ``None`` looks up the pinned engine at call time
    (:func:`default_executable`).
    """

    def __init__(
        self,
        *,
        module: str = "src",
        tests_dir: str = "tests",
        timeout_seconds: float = 60.0,
        executable: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._module = module
        self._tests_dir = tests_dir
        for value in (module, tests_dir):
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or not _as_dir(value):
                raise ValueError(f"not a directory below the workspace root: {value!r}")
        self._analysed = (_as_dir(module), _as_dir(tests_dir))
        self._timeout = timeout_seconds
        self._executable = executable
        self._clock = clock

    async def impacted(self, snapshot: ChangeSnapshot, *, catalogue: Catalogue) -> StaticResult:
        started = self._clock()
        deadline = started + self._timeout

        def result(modules=frozenset(), unknown=0, *, engine, reason=None, **why):
            elapsed_ms = max(0, int((self._clock() - started) * 1000))
            return StaticResult(
                frozenset(modules), reason is None, engine, reason, unknown, elapsed_ms, **why
            )

        if self._executable is not None:
            executable, engine = self._executable, UNVERIFIED_ENGINE
        else:
            executable, engine = default_executable(), PINNED_ENGINE
            if executable is None:
                return result(
                    engine=ENGINE_UNAVAILABLE, reason=STATIC_UNAVAILABLE, detail=_missing_engine()
                )
        if not snapshot.complete:
            return result(engine=engine, reason=STATIC_ERROR, detail="snapshot_incomplete")

        widened = _unfollowed(snapshot, *self._analysed)
        if widened is not None:
            return result(catalogue.universe, engine=engine, widened_by=widened)

        modes: list[tuple[str, ...]] = []
        if snapshot.base_sha != snapshot.head_sha:
            modes.append(("branch", f"--base-branch={snapshot.base_sha}"))
        modes.append(("unstaged",))
        runs = [
            asyncio.create_task(self._observe(executable, snapshot.workspace, deadline, *mode))
            for mode in modes
        ]
        try:
            # The closure usually settles first; a hit makes the engine's answer moot.
            closure = await self._load_closure(snapshot, deadline)
            if isinstance(closure, _Run):
                return result(engine=engine, reason=closure.reason, detail=closure.detail)
            loaded = _loaded_change(snapshot, closure)
            if loaded is not None:
                return result(catalogue.universe, engine=engine, widened_by=loaded)
            observations = await asyncio.gather(*runs)
        finally:
            for run in runs:
                run.cancel()
            await asyncio.gather(*runs, return_exceptions=True)

        roots = _roots(snapshot.workspace)
        universe = catalogue.universe
        modules: set[str] = set()
        unknown: set[str] = set()
        for observation in observations:
            for line in observation.stdout.decode("utf-8").splitlines():
                line = line.strip().replace("\\", "/")
                if not line:
                    continue
                path = _relative(line, roots)
                if path is not None and path in universe:
                    modules.add(path)
                else:
                    unknown.add(path or line)

        failed = next((o for o in observations if o.reason is not None), None)
        if failed is not None:
            if all(o.reason == STATIC_UNAVAILABLE for o in observations):
                engine = ENGINE_UNAVAILABLE
            return result(
                modules, len(unknown), engine=engine, reason=failed.reason, detail=failed.detail
            )
        return result(modules, len(unknown), engine=engine)

    async def _observe(
        self, executable: str, workspace: str, deadline: float, mode: str, *extra: str
    ) -> _Run:
        """One engine observation; its stdout is checked to be UTF-8."""
        argv = (
            executable,
            f"--module={self._module}",
            f"--tests-dir={self._tests_dir}",
            f"--root-dir={workspace}",
            f"--git-mode={mode}",
            *extra,
        )
        run = await self._run(argv, workspace, deadline, label=mode, spawn=STATIC_UNAVAILABLE)
        if run.reason is None:
            try:
                run.stdout.decode("utf-8")
            except UnicodeDecodeError:
                return _Run(reason=STATIC_ERROR, detail=f"{mode}:undecodable")
        return run

    async def _load_closure(
        self, snapshot: ChangeSnapshot, deadline: float
    ) -> frozenset[str] | _Run:
        """The paths :mod:`~src.test_selection.load_closure` prints, or the failed run."""
        if not sys.executable:
            return _Run(reason=STATIC_ERROR, detail="load_closure:no_interpreter")
        workspace = Path(snapshot.workspace)
        added = _added(snapshot)
        skip = [
            argument
            for c in snapshot.changes
            if c.path in added
            and PurePosixPath(c.path).name in LOAD_ROOTS
            and _entirely_new(workspace, c.path, added)
            for argument in ("--skip", c.path)
        ]
        # -I: the daemon's own copy of the script, never code from the workspace.
        argv = (sys.executable, "-I", str(_LOAD_CLOSURE_SCRIPT), snapshot.workspace)
        argv += (*self._analysed, *skip)
        run = await self._run(
            argv, snapshot.workspace, deadline, label="load_closure", spawn=STATIC_ERROR
        )
        if run.reason is not None:
            return run
        try:
            paths = json.loads(run.stdout)
        except ValueError:
            paths = None
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            return _Run(reason=STATIC_ERROR, detail="load_closure:bad_output")
        return frozenset(paths)

    async def _run(
        self, argv: Sequence[str], cwd: str, deadline: float, *, label: str, spawn: str
    ) -> _Run:
        """Run *argv* until *deadline*, killing its process group if it overruns or is cancelled."""
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                # stderr can quote source lines; a record keeps only the status.
                stderr=asyncio.subprocess.DEVNULL,
                # These parse workspace code; they have no use for the
                # daemon's credentials (the TypeSafe key among them).
                env=scrub_env(harness_credentials=False).env,
                # Its own process group, so a timeout also stops its children.
                start_new_session=True,
            )
        except OSError as exc:  # missing, not executable, not a program, or no cwd
            code = errno.errorcode.get(exc.errno or 0, "spawn_error")
            return _Run(reason=spawn, detail=f"{label}:{code}")
        try:
            remaining = max(0.0, deadline - self._clock())
            stdout, _ = await asyncio.wait_for(process.communicate(), remaining)
        except TimeoutError:
            return _Run(reason=STATIC_TIMEOUT, detail=label)
        finally:
            if process.returncode is None:
                await _kill_group(process)
        if process.returncode != 0:
            return _Run(reason=STATIC_ERROR, detail=f"{label}:exit_{process.returncode}")
        return _Run(stdout=stdout)


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


def _missing_engine() -> str:
    installed = engine_version()
    if installed == ENGINE_UNAVAILABLE:
        return "not_installed"
    if installed != PINNED_ENGINE:
        return f"version:{installed.removeprefix(ENGINE_DISTRIBUTION).strip()}"
    return "not_found"


def _roots(workspace: str) -> tuple[PurePosixPath, ...]:
    """The workspace as given and as the engine prints it (symlinks resolved)."""
    given = PurePosixPath(os.path.abspath(workspace))
    resolved = PurePosixPath(os.path.realpath(workspace))
    return (given,) if given == resolved else (given, resolved)


def _relative(line: str, roots: tuple[PurePosixPath, ...]) -> str | None:
    """*line* as a posix path relative to the workspace, or ``None`` if it names nothing inside."""
    path = PurePosixPath(line)
    if path.is_absolute():
        root = next((r for r in roots if path.is_relative_to(r)), None)
        if root is None:
            return None
        path = path.relative_to(root)
    if not path.parts or ".." in path.parts:
        return None
    return path.as_posix()


def _as_dir(value: str) -> str:
    # The engine accepts a dotted name for --module and --tests-dir as well as a path.
    return value.replace(".", "/").strip("/")


def _under(path: str, directory: str) -> bool:
    return path.startswith(directory + "/")


def _added(snapshot: ChangeSnapshot) -> frozenset[str]:
    return frozenset(c.path for c in snapshot.changes if c.status in ("added", "untracked"))


def _unfollowed(snapshot: ChangeSnapshot, module_dir: str, tests_dir: str) -> str | None:
    """The first change the engine cannot follow, from the snapshot alone, or ``None``."""
    analysed = (module_dir, tests_dir)
    workspace = Path(snapshot.workspace)
    added = _added(snapshot)
    for change in snapshot.changes:
        # A deleted or renamed-away path is the old side; checked first, it
        # also covers a removed or moved ``__init__.py``.
        gone = change.path if change.status == "deleted" else change.old_path
        if gone is not None and gone.endswith(".py") and any(_under(gone, d) for d in analysed):
            return f"{'deleted' if change.status == 'deleted' else 'renamed'}:{gone}"
        path = change.path
        is_init = path.endswith("/__init__.py") and any(_under(path, d) for d in analysed)
        if is_init and not (path in added and _entirely_new(workspace, path, added)):
            return f"package_init:{path}"
        if (
            change.status != "deleted"
            and path.endswith(".py")
            and _under(path, module_dir)
            and not _discoverable(workspace, module_dir, path)
        ):
            return f"unresolvable:{path}"
    return None


def _loaded_change(snapshot: ChangeSnapshot, closure: frozenset[str]) -> str | None:
    """The first changed module an ``__init__.py`` or ``conftest.py`` loads, or ``None``."""
    for change in snapshot.changes:
        path = change.path
        if change.status == "deleted" or PurePosixPath(path).name in LOAD_ROOTS:
            continue  # deletions and the roots themselves are judged elsewhere
        if path in closure:
            return f"import_time:{path}"
    return None


def _entirely_new(workspace: Path, root: str, added: frozenset[str]) -> bool:
    """Whether every module under *root*'s directory is new, so none has an old importer."""
    directory = workspace / PurePosixPath(root).parent
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        relative = PurePosixPath(os.path.relpath(dirpath, workspace))
        for name in filenames:
            if name.endswith(".py") and (relative / name).as_posix() not in added:
                return False
    return True


def _discoverable(workspace: Path, module_dir: str, path: str) -> bool:
    """Whether every directory between *module_dir* and *path* is a package.

    The engine walks the analysed package with ``pkgutil``, which descends
    only into directories holding an ``__init__.py``.
    """
    root = PurePosixPath(module_dir)
    parent = PurePosixPath(path).parent
    while parent != root and parent.is_relative_to(root):
        if not (workspace / parent / "__init__.py").is_file():
            return False
        parent = parent.parent
    return True
