"""``scripts/regenerate-api-client.sh`` must not rewrite the environment it runs in.

The script used to end with an unconditional ``pip install -e packages/aq-client``.
Run from a worktree slot, ``pip`` is the *shared* venv's, so that one line
rewrote ``site-packages/agent_queue_api_client.pth`` to the slot's path and every
process on the box — the daemon, the CLI, every other slot's tests — began
importing the client from a tree that is reset onto another branch between
tasks.  Regenerating is required after any ``src/api/models`` change, so an
ordinary worker task was enough to do it.

An editable install is a pointer at the source directory, so regenerating in
place already changes what is imported; the reinstall only ever mattered on a
box that had never installed the client.  It is therefore opt-in
(``--install``), refused in a worker session, and refuses to move an install
that currently resolves to another tree.

These tests drive a copy of the real script inside a sandbox: the generator,
``ruff`` and ``pip`` are stubs, and ``python3`` is the real interpreter run
with ``-S`` so it sees only what the test puts on ``PYTHONPATH`` — never the
venv the suite itself runs in.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "regenerate-api-client.sh"

_BOILERPLATE = (
    "README.md",
    "pyproject.toml",
    "agent_queue_api_client/__init__.py",
    "agent_queue_api_client/client.py",
    "agent_queue_api_client/errors.py",
    "agent_queue_api_client/types.py",
    "agent_queue_api_client/py.typed",
)

_FAKE_GENERATOR = """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--version" ]]; then
    echo "openapi-python-client version @VERSION@"
    exit 0
fi
out=""
while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--output-path" ]]; then out="$2"; shift; fi
    shift
done
mkdir -p "$out/agent_queue_api_client"
for name in @BOILERPLATE@; do
    echo "regenerated" > "$out/$name"
done
"""

# Stands in for both ``pip`` on PATH and ``python3 -m pip``: the tests assert
# on whether the environment was touched, not on which spelling touched it.
_FAKE_PIP_MODULE = """import os, sys
with open(os.environ["FAKE_PIP_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
"""


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _client_tree(client_dir: Path, marker: str) -> None:
    (client_dir / "agent_queue_api_client").mkdir(parents=True)
    for name in _BOILERPLATE:
        (client_dir / name).write_text(marker, encoding="utf-8")


@dataclass
class Sandbox:
    root: Path
    bin_dir: Path
    fake_site: Path
    pip_log: Path

    @property
    def client_dir(self) -> Path:
        return self.root / "packages" / "aq-client"

    def pip_calls(self) -> list[str]:
        if not self.pip_log.exists():
            return []
        return self.pip_log.read_text(encoding="utf-8").splitlines()

    def run(
        self,
        *args: str,
        importable_from: Path | None = None,
        worker: bool = False,
        path: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run the sandboxed script.

        ``importable_from`` is the directory ``agent_queue_api_client``
        currently imports from — what an editable install's ``.pth`` puts on
        ``sys.path``.  ``None`` is a box that never installed the client.
        """
        pythonpath = [str(self.fake_site)]
        if importable_from is not None:
            pythonpath.append(str(importable_from))
        env = {
            "PATH": path or f"{self.bin_dir}:/usr/bin:/bin",
            "HOME": str(self.root),
            "PYTHONPATH": os.pathsep.join(pythonpath),
            "FAKE_PIP_LOG": str(self.pip_log),
        }
        if worker:
            env["AQ_DB_SCOPE"] = "worker"
        return subprocess.run(
            [shutil.which("bash") or "/bin/bash", str(self.root / "scripts" / "regenerate-api-client.sh"), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            cwd=self.root,
            timeout=60,
        )


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    script_text = SCRIPT.read_text(encoding="utf-8")
    pinned = next(
        line.split('"')[1]
        for line in script_text.splitlines()
        if line.startswith("GENERATOR_VERSION=")
    )

    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "regenerate-api-client.sh")
    shutil.copy(
        REPO_ROOT / "scripts" / "openapi-python-client.yaml",
        root / "scripts" / "openapi-python-client.yaml",
    )
    (root / "openapi.json").write_text(
        json.dumps({"openapi": "3.1.0", "info": {"title": "t", "version": "0"}, "paths": {}}),
        encoding="utf-8",
    )
    _client_tree(root / "packages" / "aq-client", "committed")
    (root / "src" / "api").mkdir(parents=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "api" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "api" / "spec.py").write_text(
        "import json\nimport sys\nfrom pathlib import Path\n"
        "Path(sys.argv[-1]).write_text(json.dumps({'paths': {}}), encoding='utf-8')\n",
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(
        bin_dir / "openapi-python-client",
        _FAKE_GENERATOR.replace("@VERSION@", pinned).replace(
            "@BOILERPLATE@", " ".join(_BOILERPLATE)
        ),
    )
    _executable(bin_dir / "ruff", "#!/usr/bin/env bash\nexit 0\n")
    # -S keeps the suite's own venv (and whatever its editable install points
    # at today) out of the script's view; PYTHONPATH is still honoured.
    for name in ("python", "python3"):
        _executable(bin_dir / name, f'#!/usr/bin/env bash\nexec "{sys.executable}" -S "$@"\n')
    _executable(bin_dir / "pip", f'#!/usr/bin/env bash\nexec "{bin_dir}/python3" -m pip "$@"\n')

    fake_site = tmp_path / "fake-site"
    (fake_site / "pip").mkdir(parents=True)
    (fake_site / "pip" / "__init__.py").write_text("", encoding="utf-8")
    (fake_site / "pip" / "__main__.py").write_text(_FAKE_PIP_MODULE, encoding="utf-8")

    return Sandbox(root=root, bin_dir=bin_dir, fake_site=fake_site, pip_log=tmp_path / "pip.log")


def _regenerated(sandbox: Sandbox) -> bool:
    return (sandbox.client_dir / "README.md").read_text(encoding="utf-8").strip() == "regenerated"


@pytest.mark.parametrize("worker", [False, True], ids=["operator", "worker-session"])
def test_regenerating_does_not_install_the_client(sandbox: Sandbox, worker: bool):
    """The bug: a plain regeneration re-pointed the shared venv at the slot."""
    other_tree = sandbox.root.parent / "main-checkout" / "packages" / "aq-client"
    _client_tree(other_tree, "main")

    result = sandbox.run("--from-file", importable_from=other_tree, worker=worker)

    assert result.returncode == 0, result.stderr
    assert _regenerated(sandbox)
    assert sandbox.pip_calls() == []


def test_a_plain_run_says_which_tree_the_installed_client_comes_from(sandbox: Sandbox):
    """Someone about to test the regenerated client needs to know it is not the one imported."""
    other_tree = sandbox.root.parent / "main-checkout" / "packages" / "aq-client"
    _client_tree(other_tree, "main")

    result = sandbox.run("--from-file", importable_from=other_tree)

    assert result.returncode == 0, result.stderr
    assert str(other_tree) in result.stdout
    assert f"pip install -e {sandbox.client_dir}" in result.stdout


def test_a_worker_session_is_not_handed_the_command_that_repoints_the_shared_install(
    sandbox: Sandbox,
):
    """An agent that reads "to move the install here, run …" is liable to run it."""
    other_tree = sandbox.root.parent / "main-checkout" / "packages" / "aq-client"
    _client_tree(other_tree, "main")

    result = sandbox.run("--from-file", importable_from=other_tree, worker=True)

    assert result.returncode == 0, result.stderr
    assert str(other_tree) in result.stdout
    assert "pip install" not in result.stdout + result.stderr
    assert f"PYTHONPATH={sandbox.client_dir}" in result.stdout


def test_a_plain_run_needs_no_reinstall_when_the_install_already_points_here(sandbox: Sandbox):
    result = sandbox.run("--from-file", importable_from=sandbox.client_dir)

    assert result.returncode == 0, result.stderr
    assert sandbox.pip_calls() == []
    assert "already" in result.stdout
    assert "pip install -e" not in result.stdout


@pytest.mark.parametrize(
    "args", [("--from-file", "--install"), ("--install", "--from-file")], ids=["after", "before"]
)
def test_install_flag_installs_the_client_where_none_is_installed(
    sandbox: Sandbox, args: tuple[str, ...]
):
    result = sandbox.run(*args)

    assert result.returncode == 0, result.stderr
    assert _regenerated(sandbox)
    assert len(sandbox.pip_calls()) == 1
    assert f"install -e {sandbox.client_dir}" in sandbox.pip_calls()[0]


def test_install_flag_refreshes_an_install_that_already_points_here(sandbox: Sandbox):
    result = sandbox.run("--from-file", "--install", importable_from=sandbox.client_dir)

    assert result.returncode == 0, result.stderr
    assert len(sandbox.pip_calls()) == 1


def test_install_flag_is_refused_in_a_worker_session_before_anything_is_written(
    sandbox: Sandbox,
):
    result = sandbox.run("--from-file", "--install", worker=True)

    assert result.returncode != 0
    assert "AQ_DB_SCOPE=worker" in result.stderr
    assert sandbox.pip_calls() == []
    assert not _regenerated(sandbox), "a refused run must leave the committed client alone"


def test_install_flag_refuses_to_repoint_an_install_that_resolves_to_another_tree(
    sandbox: Sandbox,
):
    """A linked worktree sharing the main checkout's venv carries no AQ_DB_SCOPE."""
    other_tree = sandbox.root.parent / "main-checkout" / "packages" / "aq-client"
    _client_tree(other_tree, "main")

    result = sandbox.run("--from-file", "--install", importable_from=other_tree)

    assert result.returncode != 0
    assert str(other_tree) in result.stderr
    assert f"pip install -e {sandbox.client_dir}" in result.stderr
    assert sandbox.pip_calls() == []
    assert not _regenerated(sandbox), "a refused run must leave the committed client alone"


def test_an_unknown_argument_is_a_usage_error_not_a_daemon_fetch(sandbox: Sandbox):
    """``--instal`` used to fall through to the fetch-from-a-daemon branch."""
    result = sandbox.run("--from-file", "--instal")

    assert result.returncode == 2
    assert "--instal" in result.stderr
    assert not _regenerated(sandbox)


def test_offline_generation_uses_python3_when_python_is_absent(sandbox: Sandbox):
    """Worker images commonly omit the unversioned ``python`` command."""
    (sandbox.bin_dir / "python").unlink()

    result = sandbox.run("--offline")

    assert result.returncode == 0, result.stderr
    assert _regenerated(sandbox)


def test_checkout_venv_interpreter_takes_precedence_over_python3(sandbox: Sandbox):
    marker = sandbox.root / "venv-python-used"
    interpreter = sandbox.root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    _executable(
        interpreter,
        f'#!/usr/bin/env bash\necho used >> "{marker}"\nexec "{sys.executable}" -S "$@"\n',
    )

    result = sandbox.run("--from-file")

    assert result.returncode == 0, result.stderr
    assert set(marker.read_text(encoding="utf-8").splitlines()) == {"used"}


def test_missing_venv_and_python3_fails_before_regeneration(sandbox: Sandbox):
    minimal_bin = sandbox.root / "minimal-bin"
    minimal_bin.mkdir()
    os.symlink(shutil.which("dirname") or "/usr/bin/dirname", minimal_bin / "dirname")

    result = sandbox.run("--from-file", path=str(minimal_bin))

    assert result.returncode != 0
    assert "no Python interpreter found" in result.stderr
    assert not _regenerated(sandbox)
