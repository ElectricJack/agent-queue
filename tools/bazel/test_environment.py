"""Credential-free child environments and fail-closed Linux test isolation.

B1's runner supplies absolute declared runtime/runfiles paths; H3/H4 reuse
``TestEnvironment.child_env`` for disposable PG/daemon children. This module
never changes the parent environment or imports production ``scrub_env``.
The standalone helper is launched with the declared Python in isolated mode.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
import fcntl
import json
import math
import os
import re
import signal
import socket
import struct
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

# Enumerated Bazel protocol, deliberately not TEST_*/RUNFILES_* prefix copying.
BAZEL_VARIABLES = frozenset({
    "TEST_TARGET", "TEST_WORKSPACE", "TEST_BINARY", "TEST_SIZE", "TEST_TIMEOUT",
    "TEST_RANDOM_SEED", "TEST_SHARD_INDEX", "TEST_TOTAL_SHARDS", "TEST_RUN_NUMBER",
    "TEST_SHARD_STATUS_FILE", "XML_OUTPUT_FILE", "TEST_UNDECLARED_OUTPUTS_DIR",
    "TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR", "TEST_WARNINGS_OUTPUT_FILE",
    "TEST_LOGSPLITTER_OUTPUT_FILE", "TEST_PREMATURE_EXIT_FILE", "TEST_INFRASTRUCTURE_FAILURE_FILE",
    "RUNFILES_DIR", "RUNFILES_MANIFEST_FILE", "JAVA_RUNFILES", "PYTHON_RUNFILES", "TEST_SRCDIR",
})
_RUNFILES = frozenset({
    "RUNFILES_DIR", "RUNFILES_MANIFEST_FILE", "JAVA_RUNFILES", "PYTHON_RUNFILES", "TEST_SRCDIR",
})
_OUTPUTS = frozenset({
    "TEST_SHARD_STATUS_FILE", "XML_OUTPUT_FILE", "TEST_UNDECLARED_OUTPUTS_DIR",
    "TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR", "TEST_WARNINGS_OUTPUT_FILE",
    "TEST_LOGSPLITTER_OUTPUT_FILE", "TEST_PREMATURE_EXIT_FILE", "TEST_INFRASTRUCTURE_FAILURE_FILE",
})
# Explicit target settings cannot override the boundary or inject interpreter
# startup hooks. Disposable authentication/DSNs are created *inside* the target.
_RESERVED_PREFIXES = (
    "AQ_", "AGENT_QUEUE_", "GIT_", "TMUX", "XDG_", "PYTHON", "PYTEST", "LD_", "DYLD_",
    "BASH", "ENV", "SHELLOPTS", "SSH_", "DOCKER_", "CONTAINER_", "TEST_", "RUNFILES_",
    "CLAUDE", "CODEX", "ANTHROPIC", "OPENAI", "GOOGLE", "GEMINI", "AWS_", "AZURE_",
)
_SECRET_NAME = re.compile(r"TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL|AUTH|DSN|SESSION")


class IsolationError(RuntimeError):
    """The declared isolation contract could not be established; never fallback."""


def _absolute(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or ":" in str(path):
        raise IsolationError("declared paths must be absolute, normalized and colon-free")
    return path


def _within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


@dataclass(frozen=True)
class ReadOnlyMapping:
    """A locked sysroot directory mounted at its conventional ELF ABI path."""

    source: Path
    target: Path


@dataclass(frozen=True)
class Runtime:
    """Complete declared runtime closure; no ambient executable/library discovery.

    Include the Python standard library, native shared libraries and ELF loader
    in read_only, as well as runfiles and all symlink destinations they use.
    Paths retain their absolute spelling inside the namespace.
    """

    python: Path
    read_only: tuple[Path, ...]
    bin_dirs: tuple[Path, ...]
    library_dirs: tuple[Path, ...] = ()
    git_exec_path: Path | None = None
    shell: Path | None = None
    env_executable: Path | None = None
    sysroot: Path | None = None
    read_only_mappings: tuple[ReadOnlyMapping, ...] = ()

    def validate(self) -> None:
        if not self.bin_dirs:
            raise IsolationError("a declared toolchain PATH is required")
        for root in self.read_only:
            root = _absolute(root)
            if root == Path("/") or not root.exists():
                raise IsolationError("invalid declared input root")
        paths = (self.python, *self.bin_dirs, *self.library_dirs)
        if self.git_exec_path is not None:
            paths += (self.git_exec_path,)
        if self.shell is not None:
            paths += (self.shell,)
        if self.env_executable is not None:
            paths += (self.env_executable,)
        for path in paths:
            if not _within(_absolute(path), self.read_only) or not path.exists():
                raise IsolationError("runtime path is missing from declared inputs")
        if not self.python.is_file() or not os.access(self.python, os.X_OK):
            raise IsolationError("declared Python is not executable")
        if self.sysroot is not None:
            if (not _within(_absolute(self.sysroot), self.read_only)
                    or not _within(self.sysroot.resolve(), tuple(p.resolve() for p in self.read_only))
                    or not self.sysroot.is_dir()):
                raise IsolationError("sysroot is missing from declared locked inputs")
        targets = set()
        for mapping in self.read_only_mappings:
            source, target = _absolute(mapping.source), _absolute(mapping.target)
            if (self.sysroot is None or not source.is_dir()
                    or not source.resolve().is_relative_to(self.sysroot.resolve())
                    or not _within(source, self.read_only)):
                raise IsolationError("mapping source is outside the declared locked sysroot")
            # An additive ABI mapping can never replace workspace, /etc, /proc,
            # /dev, private home/tmp, or arbitrary runtime paths.
            if target not in {Path('/lib'), Path('/lib64'), Path('/usr/lib'), Path('/usr/lib64')}:
                raise IsolationError("mapping target must be a conventional ELF ABI directory")
            if target in targets:
                raise IsolationError("duplicate read-only mapping target")
            targets.add(target)
            if any(_within(target, (root,)) or _within(root, (target,))
                   for root in self.read_only):
                raise IsolationError("mapping target shadows declared runtime inputs")


@dataclass(frozen=True)
class TestEnvironment:
    __test__ = False
    root: Path
    runtime: Runtime
    environ: dict[str, str]
    writable: tuple[Path, ...]

    def child_env(self, settings: Mapping[str, str] | None = None) -> dict[str, str]:
        """Fresh copy for children, with only explicitly declared safe settings.

        Local service credentials/DSNs are added by H3/H4 after creation inside
        the isolated target; they must never be sourced from the caller's env.
        """
        result = dict(self.environ)
        for name, value in (settings or {}).items():
            if (not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
                    or name in result or name in {"HOME", "PATH", "TMPDIR", "TMP", "TEMP"}
                    or name.startswith(_RESERVED_PREFIXES) or _SECRET_NAME.search(name)
                    or "://" in value or "\0" in value):
                raise IsolationError(f"reserved or unsafe target setting: {name}")
            result[name] = value
        return result


@contextmanager
def test_environment(
    *,
    test_tmpdir: Path,
    runtime: Runtime,
    bazel: Mapping[str, str] | None = None,
    output_roots: Sequence[Path] = (),
    settings: Mapping[str, str] | None = None,
):
    """Allocate a private home/config/tmp tree under the supplied TEST_TMPDIR.

    ``bazel`` is an explicitly selected mapping, not an inherited environment.
    Output paths must belong to separately declared writable Bazel output roots.
    No host temporary/config directory is implicitly mounted into the child.
    """
    test_tmpdir = _absolute(test_tmpdir)
    if not test_tmpdir.is_dir():
        raise IsolationError("TEST_TMPDIR must be an existing directory")
    with tempfile.TemporaryDirectory(prefix="aq-test-", dir=test_tmpdir) as temporary:
        yield create_test_environment(
            root=Path(temporary), runtime=runtime, bazel=bazel,
            output_roots=output_roots, settings=settings,
        )


def create_test_environment(
    *,
    root: Path,
    runtime: Runtime,
    bazel: Mapping[str, str] | None = None,
    output_roots: Sequence[Path] = (),
    settings: Mapping[str, str] | None = None,
) -> TestEnvironment:
    """Populate an empty caller-owned directory using the H2 positive allowlist.

    The caller owns its lifetime. E2E worlds use this when a daemon must survive
    several independent script calls; ordinary targets use ``test_environment``.
    """
    runtime.validate()
    root = _absolute(root)
    if not root.is_dir() or any(root.iterdir()):
        raise IsolationError("environment root must be an empty private directory")
    root.chmod(0o700)
    outputs = tuple(_absolute(Path(p)) for p in output_roots)
    for path in outputs:
        if not path.is_dir() or path == Path("/"):
            raise IsolationError("output roots must be existing private directories")
    names = {
        "HOME": "home", "XDG_CONFIG_HOME": "config", "XDG_CACHE_HOME": "cache",
        "XDG_DATA_HOME": "data", "XDG_STATE_HOME": "state", "XDG_RUNTIME_DIR": "run",
        "TMPDIR": "tmp", "TMUX_TMPDIR": "tmux", "AQ_E2E_HOME": "e2e",
    }
    env = {key: str(root / directory) for key, directory in names.items()}
    for value in env.values():
        Path(value).mkdir(mode=0o700)
    config = root / "config" / "aq.yaml"
    config.write_text("{}\n", encoding="utf-8")
    config.chmod(0o600)
    env.update({
        "TEST_TMPDIR": str(root), "TMP": env["TMPDIR"], "TEMP": env["TMPDIR"],
        "AQ_CONFIG_PATH": str(config), "PATH": os.pathsep.join(map(str, runtime.bin_dirs)),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "PYTHONNOUSERSITE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    if runtime.library_dirs:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(map(str, runtime.library_dirs))
    if runtime.git_exec_path is not None:
        env["GIT_EXEC_PATH"] = str(runtime.git_exec_path)
    if runtime.shell is not None:
        env["SHELL"] = str(runtime.shell)
    for name, value in (bazel or {}).items():
        if name not in BAZEL_VARIABLES or "\0" in value:
            raise IsolationError(f"undeclared Bazel variable: {name}")
        if name in _RUNFILES and not _within(_absolute(Path(value)), runtime.read_only):
            raise IsolationError("runfiles path is outside declared inputs")
        if name in _OUTPUTS and not _within(_absolute(Path(value)), (root, *outputs)):
            raise IsolationError("Bazel output path is outside declared output roots")
        env[name] = value
    environment = TestEnvironment(root, runtime, env, (root, *outputs))
    # Prevent an input mount from exposing the parent TMPDIR or shadowing
    # the private state (and vice versa). Sibling targets share no writes.
    roots = (*runtime.read_only, *environment.writable)
    for mapping in runtime.read_only_mappings:
        if any(_within(mapping.target, (root,)) or _within(root, (mapping.target,))
               for root in environment.writable):
            raise IsolationError("mapping target shadows private writable state")
    for i, first in enumerate(roots):
        for second in roots[i + 1:]:
            if _within(first, (second,)) or _within(second, (first,)):
                raise IsolationError("declared input/output roots must not overlap")
    return TestEnvironment(root, runtime, environment.child_env(settings), environment.writable)
