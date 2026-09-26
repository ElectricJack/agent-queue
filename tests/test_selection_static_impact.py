"""The static impact adapter over pytest-impacted, and the fixture evaluation of its engine."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from src.git.manager import GitManager
from src.test_selection import static_impact
from src.test_selection.catalogue import load_catalogue
from src.test_selection.load_closure import load_closure
from src.test_selection.snapshot import take_snapshot
from src.test_selection.static_impact import (
    PINNED_ENGINE,
    STATIC_ERROR,
    STATIC_TIMEOUT,
    STATIC_UNAVAILABLE,
    UNVERIFIED_ENGINE,
    FixedStaticImpact,
    PytestImpactedAdapter,
    UnavailableStaticImpact,
    default_executable,
    engine_version,
)
from tests.selection_fixture_repo import build_fixture_repo, git

ROOT = Path(__file__).resolve().parent.parent

needs_engine = pytest.mark.skipif(
    default_executable() is None,
    reason=f"pip install -e '.[test-selection]' ({PINNED_ENGINE}) to run the fixture evaluation",
)


@pytest.fixture(autouse=True)
def _pg_backend():
    """Subprocess and git only."""


@pytest.fixture
def world(tmp_path):
    repo = build_fixture_repo(tmp_path / "repo")
    return repo, load_catalogue(repo / "tests/selection_catalogue.json")


def _engine(path, body: str):
    """A stand-in ``impacted-tests``: a Python script running *body*."""
    path.write_text(f"#!{sys.executable}\nimport json, os, sys\n{body}\n")
    path.chmod(0o755)
    return str(path)


def _recording_engine(tmp_path, *, stdout: str = "") -> tuple[str, object]:
    """An engine that appends its argv, cwd and environment keys to a log, then prints *stdout*."""
    log = tmp_path / "calls.jsonl"
    body = (
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),"
        " 'env': sorted(os.environ)}) + '\\n')\n"
        f"sys.stdout.write({stdout!r})"
    )
    return _engine(tmp_path / "recording", body), log


def _alive(pid: int) -> bool:
    """Whether *pid* still runs; a zombie awaiting its reaper counts as gone."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _calls(log) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _commit_to_base(repo, files: dict[str, str]) -> None:
    """Write *files* and make them part of ``origin/main``, the snapshot's base."""
    for relative, text in files.items():
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text)
    git(repo, "add", *files)
    git(repo, "commit", "-qm", "base change")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")


async def _impacted(repo, catalogue, **adapter):
    snap = await take_snapshot(GitManager(), str(repo))
    return await PytestImpactedAdapter(**adapter).impacted(snap, catalogue=catalogue)


def _edit_a(repo):
    (repo / "src/pkg/a.py").write_text("def alpha():\n    return 9\n")


# --------------------------------------------------------------------------
# Stand-ins and the engine pin


async def test_unavailable_and_fixed_engines_report_honestly(world):
    repo, catalogue = world
    snap = await take_snapshot(GitManager(), str(repo))
    off = await UnavailableStaticImpact().impacted(snap, catalogue=catalogue)
    assert (off.complete, off.reason, off.modules) == (False, "static_unavailable", frozenset())
    assert off.engine == "unavailable"
    fixed = await FixedStaticImpact(frozenset({"tests/test_a.py"})).impacted(
        snap, catalogue=catalogue
    )
    assert fixed.complete and fixed.modules == {"tests/test_a.py"} and fixed.reason is None
    assert fixed.engine == "fixed"


async def test_a_fixed_engine_keeps_the_catalogue_contract(world):
    repo, catalogue = world
    snap = await take_snapshot(GitManager(), str(repo))
    fixed = FixedStaticImpact({"tests/test_a.py", "tests/nope.py"}, complete=False)
    result = await fixed.impacted(snap, catalogue=catalogue)
    assert result.modules == {"tests/test_a.py"} and result.unknown_outputs == 1
    assert (result.complete, result.reason) == (False, STATIC_ERROR)


def test_pyproject_pins_the_engine_the_adapter_expects():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    extras = data["project"]["optional-dependencies"]
    pin = "pytest-impacted==" + PINNED_ENGINE.split()[-1]
    assert extras["test-selection"] == [pin]
    assert pin in extras["dev"]


def test_engine_version_names_the_package_or_says_unavailable():
    version = engine_version()
    assert version == "unavailable" or version.startswith("pytest-impacted ")


async def test_an_unpinned_engine_is_unavailable(world, monkeypatch):
    repo, catalogue = world
    monkeypatch.setattr(static_impact, "engine_version", lambda: "pytest-impacted 0.31.0")
    result = await _impacted(repo, catalogue)
    assert (result.complete, result.reason, result.detail, result.engine) == (
        False,
        STATIC_UNAVAILABLE,
        "version:0.31.0",
        "unavailable",
    )


async def test_an_uninstalled_engine_is_unavailable(world, monkeypatch):
    repo, catalogue = world
    monkeypatch.setattr(static_impact, "engine_version", lambda: "unavailable")
    result = await _impacted(repo, catalogue)
    assert (result.reason, result.detail) == (STATIC_UNAVAILABLE, "not_installed")


async def test_no_engine_command_is_unavailable(world, monkeypatch, tmp_path):
    repo, catalogue = world
    snap = await take_snapshot(GitManager(), str(repo))
    monkeypatch.setattr(static_impact, "engine_version", lambda: PINNED_ENGINE)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "empty" / "python"))
    result = await PytestImpactedAdapter().impacted(snap, catalogue=catalogue)
    assert (result.complete, result.reason, result.detail, result.engine) == (
        False,
        STATIC_UNAVAILABLE,
        "not_found",
        "unavailable",
    )


async def test_a_named_executable_is_labelled_unverified(world, tmp_path):
    repo, catalogue = world
    fake, _ = _recording_engine(tmp_path)
    _edit_a(repo)
    result = await _impacted(repo, catalogue, executable=fake)
    assert result.complete and result.engine == UNVERIFIED_ENGINE


@pytest.mark.parametrize("value", [".", "", "/", "src/../..", "../src"])
def test_a_directory_that_is_not_below_the_root_is_refused(value):
    with pytest.raises(ValueError):
        PytestImpactedAdapter(module=value)
    with pytest.raises(ValueError):
        PytestImpactedAdapter(tests_dir=value)


# --------------------------------------------------------------------------
# Running the engine: failures are incomplete, never exceptions


async def test_a_missing_executable_is_unavailable_not_an_exception(world):
    repo, catalogue = world
    result = await _impacted(repo, catalogue, executable="/nonexistent/impacted-tests")
    assert result.complete is False and result.reason == "static_unavailable"
    assert result.engine == "unavailable" and result.modules == frozenset()
    assert result.detail == "unstaged:ENOENT"


async def test_a_non_executable_file_is_unavailable(world, tmp_path):
    repo, catalogue = world
    plain = tmp_path / "impacted-tests"
    plain.write_text("not a program\n")
    result = await _impacted(repo, catalogue, executable=str(plain))
    assert (result.complete, result.reason, result.detail) == (
        False,
        STATIC_UNAVAILABLE,
        "unstaged:EACCES",
    )


async def test_a_hanging_engine_times_out_to_incomplete(world, tmp_path):
    repo, catalogue = world
    fake = _engine(tmp_path / "slow", "import time; time.sleep(5)")
    _edit_a(repo)
    started = time.monotonic()
    result = await _impacted(repo, catalogue, executable=fake, timeout_seconds=0.2)
    assert result.complete is False and result.reason == STATIC_TIMEOUT
    # The engine is killed at the deadline, not waited out.
    assert time.monotonic() - started < 4


async def test_cancelling_the_call_kills_the_engine_and_its_children(world, tmp_path):
    repo, catalogue = world
    pids = tmp_path / "pids.json"
    body = (
        "import subprocess\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pids)!r}, 'w').write(json.dumps([os.getpid(), child.pid]))\n"
        "child.wait()"
    )
    fake = _engine(tmp_path / "spawning", body)
    _edit_a(repo)
    snap = await take_snapshot(GitManager(), str(repo))
    adapter = PytestImpactedAdapter(executable=fake, timeout_seconds=120)
    call = asyncio.create_task(adapter.impacted(snap, catalogue=catalogue))
    for _ in range(1500):  # up to 30 s for a loaded box to start two interpreters
        if pids.exists() and pids.read_text():
            break
        await asyncio.sleep(0.02)
    else:
        call.cancel()
        pytest.fail("the stand-in engine never started its child")
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    engine, child = json.loads(pids.read_text())
    for _ in range(250):
        if not (_alive(engine) or _alive(child)):
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("the engine or its child outlived the cancelled call")


async def test_output_is_normalised_and_filtered_to_the_catalogue(world, tmp_path):
    repo, catalogue = world
    fake = tmp_path / "engine"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"print('{repo}/tests/test_a.py')\nprint('tests\\\\sub\\\\test_b.py')\n"
        "print('tests/nope.py')\nprint('')\n"
    )
    fake.chmod(0o755)
    _edit_a(repo)
    result = await _impacted(repo, catalogue, executable=str(fake))
    assert result.complete
    assert result.modules == {"tests/test_a.py", "tests/sub/test_b.py"}
    assert result.unknown_outputs == 1


async def test_paths_outside_the_workspace_are_unknown_not_modules(world, tmp_path):
    repo, catalogue = world
    outside = tmp_path / "elsewhere" / "tests" / "test_a.py"
    lines = [str(outside), "../repo/tests/test_c.py", "./tests/test_c.py", "tests/test_a.py\r"]
    fake, _ = _recording_engine(tmp_path, stdout="".join(f"{line}\n" for line in lines))
    _edit_a(repo)
    result = await _impacted(repo, catalogue, executable=fake)
    assert result.complete
    assert result.modules == {"tests/test_a.py", "tests/test_c.py"}
    assert result.unknown_outputs == 2


async def test_resolved_output_maps_back_through_a_symlinked_workspace(world, tmp_path):
    repo, catalogue = world
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    fake, _ = _recording_engine(tmp_path, stdout=f"{repo.resolve()}/tests/test_c.py\n")
    (repo / "src/pkg/c.py").write_text("def gamma():\n    return 9\n")
    result = await _impacted(link, catalogue, executable=fake)
    assert result.complete and result.modules == {"tests/test_c.py"}


async def test_a_clean_branch_runs_only_the_unstaged_observation(world, tmp_path):
    repo, catalogue = world
    fake, log = _recording_engine(tmp_path)
    _edit_a(repo)
    snap = await take_snapshot(GitManager(), str(repo))
    assert snap.base_sha == snap.head_sha
    adapter = PytestImpactedAdapter(executable=fake, module="src", tests_dir="tests")
    result = await adapter.impacted(snap, catalogue=catalogue)
    assert result.complete and result.modules == frozenset()
    (call,) = _calls(log)
    assert call["argv"] == [
        "--module=src",
        "--tests-dir=tests",
        f"--root-dir={repo}",
        "--git-mode=unstaged",
    ]
    assert os.path.realpath(call["cwd"]) == os.path.realpath(repo)


async def test_a_branch_runs_both_observations_against_the_merge_base(world, tmp_path):
    repo, catalogue = world
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src/pkg/c.py").write_text("def gamma():\n    return 4\n")
    git(repo, "commit", "-qam", "c")
    fake, log = _recording_engine(tmp_path)
    await _impacted(repo, catalogue, executable=fake)
    flags = ("--git-mode=", "--base-branch=")
    modes = sorted(tuple(arg for arg in c["argv"] if arg.startswith(flags)) for c in _calls(log))
    assert modes == [("--git-mode=branch", f"--base-branch={base}"), ("--git-mode=unstaged",)]


async def test_observations_are_unioned(world, tmp_path):
    repo, catalogue = world
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src/pkg/c.py").write_text("def gamma():\n    return 4\n")
    git(repo, "commit", "-qam", "c")
    body = (
        "mode = next(a for a in sys.argv if a.startswith('--git-mode='))\n"
        "print('tests/test_c.py' if mode.endswith('branch') else 'tests/test_a.py')"
    )
    fake = _engine(tmp_path / "split", body)
    result = await _impacted(repo, catalogue, executable=fake)
    assert result.complete and result.modules == {"tests/test_a.py", "tests/test_c.py"}


async def test_a_failed_observation_is_an_error_and_keeps_the_other_as_evidence(world, tmp_path):
    repo, catalogue = world
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src/pkg/c.py").write_text("def gamma():\n    return 4\n")
    git(repo, "commit", "-qam", "c")
    body = (
        "if '--git-mode=branch' in sys.argv:\n"
        "    sys.stderr.write('Traceback: secret source line\\n'); sys.exit(1)\n"
        "print('tests/test_a.py')"
    )
    fake = _engine(tmp_path / "half", body)
    result = await _impacted(repo, catalogue, executable=fake)
    assert (result.complete, result.reason) == (False, STATIC_ERROR)
    assert result.modules == {"tests/test_a.py"}
    assert result.detail == "branch:exit_1"  # never the engine's stderr


async def test_undecodable_output_is_an_error(world, tmp_path):
    repo, catalogue = world
    fake = _engine(tmp_path / "bytes", "sys.stdout.buffer.write(b'tests/test_\\xff.py\\n')")
    _edit_a(repo)
    result = await _impacted(repo, catalogue, executable=fake)
    assert (result.complete, result.reason, result.detail) == (
        False,
        STATIC_ERROR,
        "unstaged:undecodable",
    )


async def test_an_incomplete_snapshot_runs_nothing(world, tmp_path):
    repo, catalogue = world
    fake, log = _recording_engine(tmp_path, stdout="tests/test_a.py\n")
    snap = await take_snapshot(GitManager(), str(repo), base_ref="origin/absent")
    assert not snap.complete
    result = await PytestImpactedAdapter(executable=fake).impacted(snap, catalogue=catalogue)
    assert (result.complete, result.reason, result.modules) == (False, STATIC_ERROR, frozenset())
    assert result.detail == "snapshot_incomplete"
    assert _calls(log) == []


async def test_the_engine_never_sees_daemon_secrets(world, tmp_path, monkeypatch):
    repo, catalogue = world
    monkeypatch.setenv("TYPESAFE_API_KEY", "never-leaves-the-daemon")
    monkeypatch.setenv("GITHUB_TOKEN", "never-leaves-the-daemon")
    fake, log = _recording_engine(tmp_path)
    _edit_a(repo)
    await _impacted(repo, catalogue, executable=fake)
    (call,) = _calls(log)
    assert "PATH" in call["env"]
    assert "TYPESAFE_API_KEY" not in call["env"] and "GITHUB_TOKEN" not in call["env"]


async def test_a_failed_load_closure_is_an_error(world, tmp_path, monkeypatch):
    repo, catalogue = world
    monkeypatch.setattr(static_impact, "_LOAD_CLOSURE_SCRIPT", tmp_path / "missing.py")
    fake, _ = _recording_engine(tmp_path, stdout="tests/test_a.py\n")
    _edit_a(repo)
    result = await _impacted(repo, catalogue, executable=fake)
    assert (result.complete, result.reason, result.detail) == (
        False,
        STATIC_ERROR,
        "load_closure:exit_2",
    )


# --------------------------------------------------------------------------
# Changes the engine cannot follow widen S to the whole catalogue


@pytest.mark.parametrize(
    ("change", "widened_by"),
    [
        (lambda r: git(r, "mv", "src/pkg/c.py", "src/pkg/e.py"), "renamed:src/pkg/c.py"),
        (lambda r: git(r, "rm", "-q", "src/pkg/c.py"), "deleted:src/pkg/c.py"),
        (
            lambda r: git(r, "rm", "-q", "tests/test_docs_scan.py"),
            "deleted:tests/test_docs_scan.py",
        ),
        (
            lambda r: (r / "src/pkg/__init__.py").write_text("X = 1\n"),
            "package_init:src/pkg/__init__.py",
        ),
        (
            lambda r: (r / "tests/sub/__init__.py").write_text("X = 1\n"),
            "package_init:tests/sub/__init__.py",
        ),
        (
            lambda r: (r / "src/loose").mkdir() or (r / "src/loose/x.py").write_text("X = 1\n"),
            "unresolvable:src/loose/x.py",
        ),
    ],
    ids=[
        "rename",
        "delete-source",
        "delete-test",
        "package-init",
        "tests-package-init",
        "no-package",
    ],
)
async def test_changes_the_engine_cannot_follow_widen_to_the_catalogue(
    world, tmp_path, change, widened_by
):
    repo, catalogue = world
    fake, log = _recording_engine(tmp_path, stdout="tests/test_a.py\n")
    change(repo)
    result = await _impacted(repo, catalogue, executable=fake)
    assert (result.complete, result.reason, result.widened_by) == (True, None, widened_by)
    assert result.modules == catalogue.universe
    assert _calls(log) == []  # decided from the snapshot: the engine is not run


async def test_a_committed_rename_on_a_branch_widens_too(world, tmp_path):
    repo, catalogue = world
    git(repo, "checkout", "-q", "-b", "feature")
    git(repo, "mv", "src/pkg/c.py", "src/pkg/e.py")
    git(repo, "commit", "-qm", "mv")
    fake, _ = _recording_engine(tmp_path)
    result = await _impacted(repo, catalogue, executable=fake)
    assert result.complete and result.widened_by == "renamed:src/pkg/c.py"


async def test_an_init_added_beside_existing_modules_widens(world, tmp_path):
    """A namespace directory becoming a package: its old importers now run the new ``__init__``."""
    repo, catalogue = world
    _commit_to_base(repo, {"src/loose/x.py": "X = 1\n"})
    (repo / "src/loose/__init__.py").write_text("")
    fake, _ = _recording_engine(tmp_path)
    result = await _impacted(repo, catalogue, executable=fake)
    assert result.widened_by == "package_init:src/loose/__init__.py"


@pytest.mark.parametrize(
    ("base", "edited"),
    [
        ({"src/pkg/__init__.py": "from . import c\n"}, "src/pkg/c.py"),
        # Transitively, through a function-local import of a module the init loads.
        ({"src/pkg/__init__.py": "from . import b\n"}, "src/pkg/a.py"),
        (
            {
                "tests/sub/conftest.py": (
                    "import pytest\n\nfrom src.pkg.c import gamma  # noqa: F401\n\n\n"
                    "@pytest.fixture\ndef beta_expected():\n    return 2\n"
                )
            },
            "src/pkg/c.py",
        ),
    ],
    ids=["package-init-import", "transitive-function-local", "conftest-import"],
)
async def test_a_module_loaded_by_an_init_or_conftest_widens(world, tmp_path, base, edited):
    repo, catalogue = world
    _commit_to_base(repo, base)
    (repo / edited).write_text((repo / edited).read_text() + "\nY = 2\n")
    fake, _ = _recording_engine(tmp_path, stdout="tests/test_c.py\n")
    result = await _impacted(repo, catalogue, executable=fake)
    assert (result.complete, result.widened_by) == (True, f"import_time:{edited}")
    assert result.modules == catalogue.universe


def _new_package(repo):
    (repo / "src/newpkg/sub").mkdir(parents=True)
    (repo / "src/newpkg/__init__.py").write_text("from . import mod\n")
    (repo / "src/newpkg/mod.py").write_text("from src.pkg.c import gamma\n")
    (repo / "src/newpkg/sub/__init__.py").write_text("")


@pytest.mark.parametrize(
    "change",
    [
        lambda r: (r / "src/pkg/new.py").write_text("X = 1\n"),  # untracked module
        _new_package,  # its own modules are all changes: no old importer to lose
        lambda r: git(r, "rm", "-q", "docs/README.md"),  # non-Python: the rule map owns it
        lambda r: (r / "src/pkg/data.json").write_text('{"k": 2}\n'),
        lambda r: (r / "tests/sub/conftest.py").write_text("import pytest\n"),  # engine follows
    ],
    ids=["added-module", "new-package", "deleted-doc", "data-file", "conftest-edit"],
)
async def test_changes_the_engine_can_follow_stay_its_answer(world, tmp_path, change):
    repo, catalogue = world
    fake, log = _recording_engine(tmp_path, stdout="tests/test_a.py\n")
    change(repo)
    result = await _impacted(repo, catalogue, executable=fake)
    assert result.complete and result.reason is None and result.widened_by is None
    assert result.modules == {"tests/test_a.py"} and len(_calls(log)) == 1


# --------------------------------------------------------------------------
# The load closure


def test_the_fixture_load_closure_is_its_roots(tmp_path):
    repo = build_fixture_repo(tmp_path / "repo")
    assert load_closure(repo, ["src", "tests"]) == {
        "src/pkg/__init__.py",
        "tests/conftest.py",
        "tests/sub/__init__.py",
        "tests/sub/conftest.py",
    }


def test_the_load_closure_follows_relative_absolute_and_src_layout_imports(tmp_path):
    files = {
        "src/app/__init__.py": "from .core import run\n",
        "src/app/core.py": "import app.util\n\ndef run():\n    from ..app import late\n",
        "src/app/util.py": "",
        "src/app/late.py": "",
        "src/app/unused.py": "",
        "tests/conftest.py": "from tests.helpers import fixture_data\n",
        "tests/helpers.py": "from src.app import util\n",
        "tests/test_x.py": "",
    }
    for relative, text in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text)
    closure = load_closure(tmp_path, ["src/app", "tests"])
    assert closure == {
        "src/app/__init__.py",
        "src/app/core.py",
        "src/app/util.py",  # as ``app.util``: src layout
        "src/app/late.py",  # function-local, relative two levels up
        "tests/conftest.py",
        "tests/helpers.py",
    }
    # A skipped root is no longer a starting point, but stays reachable.
    assert load_closure(tmp_path, ["src/app", "tests"], skip=["tests/conftest.py"]) == {
        "src/app/__init__.py",
        "src/app/core.py",
        "src/app/util.py",
        "src/app/late.py",
    }


def test_the_load_closure_script_prints_json(tmp_path):
    repo = build_fixture_repo(tmp_path / "repo")
    script = ROOT / "src/test_selection/load_closure.py"
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            str(script),
            str(repo),
            "src",
            "tests",
            "--skip",
            "tests/conftest.py",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert json.loads(out) == [
        "src/pkg/__init__.py",
        "tests/sub/__init__.py",
        "tests/sub/conftest.py",
    ]


# --------------------------------------------------------------------------
# Fixture evaluation of the real engine


def _engine_names(repo, *mode: str) -> set[str]:
    """What the installed engine prints for *repo*, made relative: the raw observation."""
    out = subprocess.run(
        [
            default_executable(),
            "--module=src",
            "--tests-dir=tests",
            f"--root-dir={repo}",
            *mode,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    root = str(repo.resolve()) + "/"
    return {line.removeprefix(root) for line in out.splitlines() if line}


@needs_engine
class TestFixtureEvaluation:
    """Spec §5.1: the library's input semantics, verified on a repo shaped like ours.

    Each test records what pytest-impacted 0.30.0 returned on the fixture, so a
    version whose semantics differ fails here before any selection trusts it.
    """

    async def test_a_dirty_edit_reaches_a_function_local_relative_importer(self, world):
        repo, catalogue = world
        (repo / "src/pkg/a.py").write_text("def alpha():\n    return 2\n")
        result = await _impacted(repo, catalogue)
        assert result.complete, result.reason
        # b imports a inside a function, relatively: ``from .a import alpha``.
        assert result.modules == {"tests/test_a.py", "tests/sub/test_b.py"}
        assert result.unknown_outputs == 0 and result.widened_by is None
        assert result.engine == PINNED_ENGINE

    async def test_a_staged_edit_is_a_dirty_change_too(self, world):
        repo, catalogue = world
        (repo / "src/pkg/a.py").write_text("def alpha():\n    return 2\n")
        git(repo, "add", "src/pkg/a.py")
        result = await _impacted(repo, catalogue)
        assert result.complete and result.modules == {"tests/test_a.py", "tests/sub/test_b.py"}

    async def test_a_test_inside_a_package_is_found(self, world):
        repo, catalogue = world
        (repo / "src/pkg/b.py").write_text("def beta():\n    return 2\n")
        result = await _impacted(repo, catalogue)
        assert result.complete and result.modules == {"tests/sub/test_b.py"}

    async def test_an_edited_test_module_is_its_own_impact(self, world):
        repo, catalogue = world
        path = repo / "tests/test_c.py"
        path.write_text(path.read_text() + "\n")
        result = await _impacted(repo, catalogue)
        assert result.complete and result.modules == {"tests/test_c.py"}

    async def test_a_committed_change_on_a_branch_is_seen_through_the_merge_base(self, world):
        repo, catalogue = world
        git(repo, "checkout", "-q", "-b", "feature")
        (repo / "src/pkg/c.py").write_text("def gamma():\n    return 4\n")
        git(repo, "commit", "-qam", "c")
        base = git(repo, "merge-base", "origin/main", "HEAD").strip()
        assert _engine_names(repo, "--git-mode=unstaged") == set()  # each mode sees only its part
        assert _engine_names(repo, "--git-mode=branch", f"--base-branch={base}") == {
            "tests/test_c.py"
        }
        result = await _impacted(repo, catalogue)
        assert result.complete and result.modules == {"tests/test_c.py"}

    async def test_committed_and_dirty_observations_are_unioned(self, world):
        repo, catalogue = world
        git(repo, "checkout", "-q", "-b", "feature")
        (repo / "src/pkg/c.py").write_text("def gamma():\n    return 4\n")
        git(repo, "commit", "-qam", "c")
        (repo / "src/pkg/a.py").write_text("def alpha():\n    return 2\n")
        result = await _impacted(repo, catalogue)
        assert result.complete
        assert result.modules == {"tests/test_a.py", "tests/sub/test_b.py", "tests/test_c.py"}

    async def test_rename_and_delete_do_not_crash_and_widen_rather_than_narrow(self, world):
        repo, catalogue = world
        git(repo, "mv", "src/pkg/c.py", "src/pkg/e.py")
        git(repo, "rm", "-q", "tests/test_docs_scan.py")
        # Observed: the engine drops a path that no longer exists and builds its
        # graph from the new tree only, so c.py's importer is lost.
        assert _engine_names(repo, "--git-mode=unstaged") == set()
        result = await _impacted(repo, catalogue)
        assert result.complete and "tests/test_c.py" in result.modules
        assert result.widened_by == "renamed:src/pkg/c.py"

    async def test_a_deleted_module_loses_its_importers_in_the_engine(self, world):
        repo, catalogue = world
        git(repo, "rm", "-q", "src/pkg/a.py")
        assert _engine_names(repo, "--git-mode=unstaged") == set()  # observed: test_a, test_b lost
        result = await _impacted(repo, catalogue)
        assert result.widened_by == "deleted:src/pkg/a.py"
        assert {"tests/test_a.py", "tests/sub/test_b.py"} <= result.modules

    async def test_a_package_init_edit_reaches_no_submodule_importer(self, world):
        repo, catalogue = world
        (repo / "src/pkg/__init__.py").write_text("X = 1\n")
        # Observed: ``from src.pkg.a import alpha`` makes no edge to ``src.pkg``.
        assert _engine_names(repo, "--git-mode=unstaged") == set()
        result = await _impacted(repo, catalogue)
        assert result.widened_by == "package_init:src/pkg/__init__.py"

    async def test_a_module_a_package_init_loads_reaches_only_its_importers(self, world):
        repo, catalogue = world
        _commit_to_base(repo, {"src/pkg/__init__.py": "from . import c\n"})
        (repo / "src/pkg/c.py").write_text("def gamma():\n    return 4\n")
        # Observed: test_a and test_b run src/pkg/__init__.py, and so c.py, but
        # the engine names only c's direct importer.
        assert _engine_names(repo, "--git-mode=unstaged") == {"tests/test_c.py"}
        result = await _impacted(repo, catalogue)
        assert result.widened_by == "import_time:src/pkg/c.py"
        assert result.modules == catalogue.universe

    async def test_a_scoped_conftest_edit_is_not_something_the_engine_proves(self, world):
        repo, catalogue = world
        (repo / "tests/sub/conftest.py").write_text(
            "import pytest\n\n@pytest.fixture\ndef thing():\n    return 2\n"
        )
        result = await _impacted(repo, catalogue)
        # Observed: the engine names every module under the conftest's directory,
        # tests/sub/__init__.py and the conftest itself included (both unknown to
        # the catalogue). The rule map (Task 3) still owns conftests.
        assert result.complete
        assert result.modules == {"tests/sub/test_b.py"} and result.unknown_outputs == 2

    async def test_a_data_file_edit_is_invisible_to_the_engine(self, world):
        repo, catalogue = world
        (repo / "src/pkg/data.json").write_text('{"k": 2}\n')
        result = await _impacted(repo, catalogue)
        # No static graph proves absence of data-file effects (spec §5.1): the
        # rule map owns data paths.
        assert result.complete and result.modules == frozenset()

    async def test_a_clean_tree_is_complete_and_empty(self, world):
        repo, catalogue = world
        result = await _impacted(repo, catalogue)
        assert result.complete and result.modules == frozenset() and result.unknown_outputs == 0

    def test_engine_version_is_pinned(self):
        assert engine_version() == "pytest-impacted 0.30.0" == PINNED_ENGINE
