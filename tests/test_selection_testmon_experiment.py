"""pytest-testmon fixture experiment: recorded evidence for spec §5.2, never a selector.

Smart test selection keeps dynamic impact as a separately measured experiment
(spec ``2026-09-24-smart-test-selection-2`` §5.2, plan Task 14).  This module
runs pytest-testmon in an isolated fixture project with AQ's own pytest
``addopts`` (the default marker deselect) and ``aq test``'s xdist shape
(``-n 2 --dist loadfile``), and asserts what it actually selects: node ids
from ``-rA``, not exit status.

It ships no deselection and changes no runner behaviour.  It is ``slow`` so the
default arm never runs it, and it skips unless the optional
``test-selection-dynamic`` extra (``pytest-testmon==2.2.0``) is importable::

    aq test --aq-all-markers tests/test_selection_testmon_experiment.py -p no:xdist -s

Each test prints one ``[testmon-experiment]`` JSON line of observations (node
ids, timings, ``.testmondata`` size) for the close summary.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow]

pytest.importorskip("testmon")

REPO_ROOT = Path(__file__).resolve().parents[1]

#: ``aq test`` adds the box's worker cap plus ``--dist loadfile``; two workers
#: are enough to put collection and execution on xdist workers.
XDIST = ("-n", "2", "--dist", "loadfile")

A_EDITED = "def alpha():\n    value = 1\n    return value\n"

FIXTURE = {
    "src/pkg/__init__.py": "",
    "src/pkg/a.py": "def alpha():\n    return 1\n",
    "src/pkg/b.py": "def beta():\n    return 2\n",
    "src/pkg/c.py": "from pkg.a import alpha\n\n\ndef gamma():\n    return alpha() + 2\n",
    "tests/test_alpha.py": (
        "from pkg.a import alpha\n\n\n"
        "def test_alpha():\n    assert alpha() == 1\n\n\n"
        "def test_alpha_twice():\n    assert alpha() + alpha() == 2\n"
    ),
    "tests/test_beta.py": (
        "from pkg.b import beta\n\n\n"
        "def test_beta():\n    assert beta() == 2\n\n\n"
        "def test_beta_alpha_label():\n"
        '    """Matches ``-k alpha`` by name only; it executes b, never a."""\n'
        "    assert beta() > 1\n"
    ),
    "tests/test_gamma.py": (
        "from pkg.c import gamma\n\n\ndef test_gamma():\n    assert gamma() == 3\n"
    ),
    # Outside the default arm: AQ's addopts deselect it before it can execute.
    "tests/test_alpha_slow.py": (
        "import pytest\n\nfrom pkg.a import alpha\n\n"
        "pytestmark = pytest.mark.slow\n\n\n"
        "def test_alpha_slow():\n    assert alpha() == 1\n"
    ),
    # The shape of tests/test_v1_removal.py and the other rules.critical /
    # source_scanning modules: it reads a source file as text and executes none
    # of it, so its only executed dependency is its own module.
    "tests/test_source_scan.py": (
        "from pathlib import Path\n\n"
        'SOURCE = Path(__file__).resolve().parents[1] / "src" / "pkg" / "a.py"\n\n\n'
        "def test_a_carries_no_print():\n"
        '    assert "print(" not in SOURCE.read_text()\n'
    ),
}

DEFAULT_ARM = {
    "tests/test_alpha.py::test_alpha",
    "tests/test_alpha.py::test_alpha_twice",
    "tests/test_beta.py::test_beta",
    "tests/test_beta.py::test_beta_alpha_label",
    "tests/test_gamma.py::test_gamma",
    "tests/test_source_scan.py::test_a_carries_no_print",
}
SLOW = "tests/test_alpha_slow.py::test_alpha_slow"
SOURCE_SCAN = "tests/test_source_scan.py::test_a_carries_no_print"
#: Tests whose executed code includes ``pkg.a.alpha``.
DEPENDS_ON_A = {
    "tests/test_alpha.py::test_alpha",
    "tests/test_alpha.py::test_alpha_twice",
    "tests/test_gamma.py::test_gamma",
}

_OUTCOME = re.compile(r"^(PASSED|FAILED|ERROR|XPASS|XFAIL) (\S+::\S+)", re.MULTILINE)
#: xdist prints "N workers [M items]" and no "deselected" count; serial runs
#: print "collected N items / K deselected / M selected".
_ITEMS = re.compile(r"\[(\d+) items?\]|(\d+) selected|collected (\d+) items?")


@dataclass(frozen=True)
class Run:
    args: tuple[str, ...]
    returncode: int
    output: str
    seconds: float

    @property
    def ran(self) -> set[str]:
        return {m.group(2) for m in _OUTCOME.finditer(self.output)}

    @property
    def failed(self) -> set[str]:
        return {m.group(2) for m in _OUTCOME.finditer(self.output) if m.group(1) != "PASSED"}

    @property
    def items(self) -> int | None:
        """How many items pytest reported it would run, after every deselection."""
        found = _ITEMS.findall(self.output)
        return int(next(n for n in found[-1] if n)) if found else None

    @property
    def testmon_header(self) -> str:
        lines = [ln for ln in self.output.splitlines() if ln.startswith("testmon:")]
        return lines[0] if lines else ""

    def summary(self) -> dict:
        return {
            "args": " ".join(self.args),
            "exit": self.returncode,
            "ran": sorted(self.ran),
            "items": self.items,
            "header": self.testmon_header,
            "seconds": round(self.seconds, 2),
        }


def _aq_ini_options() -> dict:
    """AQ's real ``[tool.pytest.ini_options]``: the marker deselect and markers."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["pytest"]["ini_options"]


def _write_project(root: Path) -> None:
    ini = _aq_ini_options()
    pyproject = "\n".join(
        [
            "[tool.pytest.ini_options]",
            'testpaths = ["tests"]',
            'pythonpath = ["src"]',
            f"addopts = {json.dumps(ini['addopts'])}",
            f"markers = {json.dumps(ini['markers'])}",
            "",
        ]
    )
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    for rel, text in FIXTURE.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="aq",
        GIT_AUTHOR_EMAIL="aq@example.invalid",
        GIT_COMMITTER_NAME="aq",
        GIT_COMMITTER_EMAIL="aq@example.invalid",
    )
    return env


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, env=_git_env(), check=True, capture_output=True, timeout=60
    )


def _child_env(datafile: Path) -> dict[str, str]:
    """The parent's environment minus anything that would steer the child pytest."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "TESTMON_", "COV_CORE_", "COVERAGE_"))
    }
    env["TESTMON_DATAFILE"] = str(datafile)
    return env


def _run(root: Path, datafile: Path, *args: str, xdist: bool = True, cache: bool = False) -> Run:
    """One child pytest in ``root``; the plan's shape unless ``xdist``/``cache`` say otherwise."""
    shape = XDIST if xdist else ("-p", "no:xdist")
    argv = (*shape, *(() if cache else ("-p", "no:cacheprovider")), "-rA", *args)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *argv],
        cwd=root,
        env=_child_env(datafile),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    return Run(argv, proc.returncode, proc.stdout + proc.stderr, time.monotonic() - started)


def _pytest(root: Path, datafile: Path, *args: str, **shape: bool) -> Run:
    run = _run(root, datafile, *args, **shape)
    assert run.returncode == 0, run.output
    assert not run.failed, run.output
    return run


def _recorded(datafile: Path) -> dict:
    """Read ``.testmondata`` read-only: recorded tests, files and environment."""
    con = sqlite3.connect(f"{datafile.as_uri()}?mode=ro", uri=True)
    try:
        tests = sorted(row[0] for row in con.execute("SELECT test_name FROM test_execution"))
        files = sorted({row[0] for row in con.execute("SELECT filename FROM file_fp")})
        envs = con.execute(
            "SELECT environment_name, python_version, system_packages FROM environment"
        ).fetchall()
    finally:
        con.close()
    return {
        "tests": tests,
        "files": files,
        "environments": [
            {"name": name, "python": python, "packages_chars": len(packages or "")}
            for name, python, packages in envs
        ],
    }


def _datafile_bytes(datafile: Path) -> int:
    return sum(
        p.stat().st_size
        for p in (datafile, Path(f"{datafile}-wal"), Path(f"{datafile}-shm"))
        if p.exists()
    )


def _copy_data(src: Path, dst: Path) -> Path:
    for suffix in ("", "-wal", "-shm"):
        if Path(f"{src}{suffix}").exists():
            shutil.copyfile(f"{src}{suffix}", f"{dst}{suffix}")
    return dst


def _edit(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    # A later mtime, even on a coarse clock, so no fast path can skip the change.
    stamp = time.time() + 5
    os.utime(path, (stamp, stamp))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    # A neutral basename: pytest keywords include the rootdir node's name, and
    # tmp_path's carries the test name, which would make ``-k alpha`` match all.
    root = tmp_path / "proj"
    root.mkdir()
    _write_project(root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root


@pytest.fixture
def baseline(project: Path, tmp_path: Path) -> Path:
    """A recording under the AQ marker arm and xdist, before any change."""
    datafile = tmp_path / "baseline.testmondata"
    _pytest(project, datafile, "--testmon")
    return datafile


@pytest.fixture
def observe(request):
    observations: dict = {}
    yield observations
    print(f"\n[testmon-experiment] {request.node.name}: {json.dumps(observations, sort_keys=True)}")


def test_aq_marker_addopts_force_noselect_under_xdist(project, tmp_path, observe):
    """AQ's ``-m`` deselect alone turns testmon selection off: it records, never selects.

    The command is the plan's: an explicit ``-m "not slow"`` on top of the
    addopts ``-m``; either one sets ``markexpr``, which forces noselect.
    """
    datafile = tmp_path / "noselect.testmondata"
    command = ("--testmon", "-m", "not slow")

    plain = _pytest(project, datafile, "-m", "not slow")
    first = _pytest(project, datafile, *command)
    first_bytes = _datafile_bytes(datafile)
    second = _pytest(project, datafile, *command)
    recorded = _recorded(datafile)
    _edit(project / "src/pkg/a.py", A_EDITED)
    after_edit = _pytest(project, datafile, *command)
    addopts_only = _pytest(project, datafile, "--testmon")
    serial_datafile = tmp_path / "serial.testmondata"
    serial = _pytest(project, serial_datafile, *command, xdist=False)
    serial_recorded = _recorded(serial_datafile)

    assert "selection automatically deactivated because -m was used" in first.testmon_header
    # Recording saw every default-arm test run on xdist workers ...
    assert first.ran == DEFAULT_ARM
    assert DEFAULT_ARM <= set(recorded["tests"])
    # ... and testmon deselects nothing: unchanged tree, changed tree, addopts-only
    # -m.  The only deselection is the marker's, which drops the slow test.
    assert second.ran == DEFAULT_ARM
    assert after_edit.ran == DEFAULT_ARM
    assert addopts_only.ran == DEFAULT_ARM
    assert "-m was used" in addopts_only.testmon_header
    # Recorded node ids: only executed tests on xdist.  The marker-deselected slow
    # test is registered by name (never executed) only by a serial recording:
    # testmon syncs collected tests on the controller, which under xdist collects
    # nothing.  Either way it has no executed dependencies.
    assert recorded["tests"] == sorted(DEFAULT_ARM)
    assert serial.ran == DEFAULT_ARM
    assert serial_recorded["tests"] == sorted(DEFAULT_ARM | {SLOW})

    observe.update(
        runs=[r.summary() for r in (plain, first, second, after_edit, addopts_only, serial)],
        recorded_tests=recorded["tests"],
        recorded_files=recorded["files"],
        environments=recorded["environments"],
        serial_recorded_tests=serial_recorded["tests"],
        slow_test_recorded_without_running={
            "xdist": SLOW in recorded["tests"],
            "serial": SLOW in serial_recorded["tests"],
        },
        datafile_bytes_after_first=first_bytes,
        datafile_bytes_final=_datafile_bytes(datafile),
    )


def test_forceselect_under_aq_markers_selects_the_executed_dependents(
    project, baseline, tmp_path, observe
):
    """``--testmon-forceselect`` is the only way to select under AQ's marker addopts.

    Compares collection-only selection (``--co``, no recording) with actual
    selected execution, on xdist and serially, from copies of one baseline.
    """
    unchanged = _pytest(
        project, _copy_data(baseline, tmp_path / "unchanged"), "--testmon-forceselect"
    )
    _edit(project / "src/pkg/a.py", A_EDITED)
    collect_only = _pytest(
        project,
        _copy_data(baseline, tmp_path / "co"),
        "--testmon-forceselect",
        "--testmon-nocollect",
        "--co",
        "-q",
    )
    xdist_run = _pytest(project, _copy_data(baseline, tmp_path / "x"), "--testmon-forceselect")
    serial_run = _pytest(
        project, _copy_data(baseline, tmp_path / "s"), "--testmon-forceselect", xdist=False
    )
    collected = {ln.strip() for ln in collect_only.output.splitlines() if "::" in ln}

    # No change: every recorded test is stable and nothing runs (exit 0, not 5).
    assert unchanged.ran == set()
    # The change to pkg.a selects exactly the tests that executed alpha, on
    # xdist and serially alike; collection-only agrees with execution.
    assert xdist_run.ran == DEPENDS_ON_A
    assert serial_run.ran == DEPENDS_ON_A
    assert collected == DEPENDS_ON_A
    # The source scanner reads a.py but executes none of it: testmon drops it.
    assert SOURCE_SCAN not in xdist_run.ran
    # The slow test is never selected into the default arm, affected or not.
    assert SLOW not in xdist_run.ran

    observe.update(
        runs=[r.summary() for r in (unchanged, collect_only, xdist_run, serial_run)],
        collect_only_selected=sorted(collected),
    )


def test_keyword_forces_noselect_and_forceselect_intersects(project, baseline, tmp_path, observe):
    """``-k`` alone runs every keyword match; with forceselect, only affected matches."""
    _edit(project / "src/pkg/a.py", A_EDITED)
    keyword = _pytest(project, _copy_data(baseline, tmp_path / "k"), "--testmon", "-k", "alpha")
    forced = _pytest(
        project,
        _copy_data(baseline, tmp_path / "kf"),
        "--testmon-forceselect",
        "-k",
        "alpha",
    )
    matches = {
        "tests/test_alpha.py::test_alpha",
        "tests/test_alpha.py::test_alpha_twice",
        "tests/test_beta.py::test_beta_alpha_label",
    }

    assert "-k was used" in keyword.testmon_header
    assert keyword.ran == matches
    assert forced.ran == matches & DEPENDS_ON_A

    observe.update(runs=[keyword.summary(), forced.summary()])


def test_unknown_tests_are_selected(project, baseline, tmp_path, observe):
    """A test testmon never executed is never stable, so forceselect always runs it.

    Includes the slow-marked test: never executed under the default arm, it is
    absent from an xdist recording and a name-only placeholder in a serial one,
    and both select it once ``-m ""`` makes it eligible.
    """
    serial_baseline = tmp_path / "serial.testmondata"
    _pytest(project, serial_baseline, "--testmon", xdist=False)
    (project / "tests/test_delta.py").write_text(
        "from pkg.b import beta\n\n\ndef test_delta():\n    assert beta() == 2\n",
        encoding="utf-8",
    )
    default_arm = _pytest(project, _copy_data(baseline, tmp_path / "u"), "--testmon-forceselect")
    # ``aq test --aq-all-markers`` emits ``-m ""``: an empty markexpr keeps
    # selection on, and the whole marker universe becomes eligible.
    all_markers = _pytest(
        project, _copy_data(baseline, tmp_path / "all"), "--testmon", "-m", "", cache=True
    )

    from_serial = _pytest(
        project, _copy_data(serial_baseline, tmp_path / "su"), "--testmon-forceselect", "-m", ""
    )

    assert default_arm.ran == {"tests/test_delta.py::test_delta"}
    assert "deactivated" not in all_markers.testmon_header
    assert all_markers.ran == {"tests/test_delta.py::test_delta", SLOW}
    assert from_serial.ran == {"tests/test_delta.py::test_delta", SLOW}

    observe.update(runs=[default_arm.summary(), all_markers.summary(), from_serial.summary()])


def test_selecting_testmon_needs_the_cacheprovider_plugin(project, baseline, tmp_path, observe):
    """testmon 2.2.0 reads the ``--lf`` option, which only the cacheprovider registers.

    Under AQ's addopts the ``-m`` reason returns first and hides this; with no
    ``-k``/``-m`` (``aq test --aq-all-markers``), ``-p no:cacheprovider`` is an
    INTERNALERROR (exit 3) before any test runs.  forceselect skips the check.
    """
    crashed = _run(project, _copy_data(baseline, tmp_path / "nc"), "--testmon", "-m", "")
    forced = _pytest(
        project, _copy_data(baseline, tmp_path / "ncf"), "--testmon-forceselect", "-m", ""
    )

    assert crashed.returncode == 3
    assert "KeyError: 'lf'" in crashed.output
    assert forced.ran == {SLOW}

    observe.update(runs=[crashed.summary(), forced.summary()])


def test_artifact_is_portable_to_a_second_clone_at_the_same_commit(
    project, baseline, tmp_path, observe
):
    """A copied ``.testmondata`` selects the same tests in a fresh clone.

    The clone has new mtimes and a different absolute root; testmon keys on
    rootdir-relative paths and content hashes, so nothing reads as changed.
    """
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(project), str(clone))
    clone_unchanged = _pytest(clone, _copy_data(baseline, tmp_path / "cu"), "--testmon-forceselect")
    for root in (project, clone):
        _edit(root / "src/pkg/a.py", A_EDITED)
    origin = _pytest(project, _copy_data(baseline, tmp_path / "o"), "--testmon-forceselect")
    copied = _pytest(clone, _copy_data(baseline, tmp_path / "c"), "--testmon-forceselect")

    assert clone_unchanged.ran == set()
    assert origin.ran == copied.ran == DEPENDS_ON_A

    observe.update(
        runs=[clone_unchanged.summary(), origin.summary(), copied.summary()],
        baseline_bytes=_datafile_bytes(baseline),
    )


def test_a_mandatory_module_survives_testmon_only_by_disabling_selection(
    project, baseline, tmp_path, observe
):
    """No pytest argument keeps one mandatory module while testmon deselects others.

    ``tests/test_source_scan.py`` stands in for a ``rules.critical`` /
    source-scanning module that testmon drops after a change it cannot see.
    Naming its file does not protect it; naming its node id protects it only by
    forcing noselect for the whole run; forceselect deselects even the node id.
    Spec §5.2 therefore keeps mandatory modules out of plugin deselection
    entirely, rather than adding node ids behind the caller's back.
    """
    _edit(project / "src/pkg/a.py", A_EDITED)
    by_file = _pytest(
        project,
        _copy_data(baseline, tmp_path / "f"),
        "--testmon-forceselect",
        "tests",
        "tests/test_source_scan.py",
    )
    # ``-m ""`` so the node id, not AQ's marker ``-m``, is what forces noselect.
    by_node = _pytest(
        project,
        _copy_data(baseline, tmp_path / "n"),
        "--testmon",
        "-m",
        "",
        "tests",
        SOURCE_SCAN,
        cache=True,
    )
    forced_node = _pytest(
        project,
        _copy_data(baseline, tmp_path / "fn"),
        "--testmon-forceselect",
        "tests",
        SOURCE_SCAN,
    )

    assert by_file.ran == DEPENDS_ON_A
    assert "you selected tests manually" in by_node.testmon_header
    assert by_node.ran == DEFAULT_ARM | {SLOW}
    assert forced_node.ran == DEPENDS_ON_A

    observe.update(runs=[by_file.summary(), by_node.summary(), forced_node.summary()])
