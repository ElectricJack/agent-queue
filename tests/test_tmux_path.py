"""The validated PATH must reach the actual pane process, not just tmux metadata."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

if os.name != "posix" or shutil.which("tmux") is None:
    pytest.skip("requires POSIX and tmux", allow_module_level=True)

from src.sessions.provider import SessionSpec
from src.sessions.tmux import TmuxCommandError, TmuxProvider

pytestmark = pytest.mark.tmux


@pytest.fixture
async def isolated_tmux(tmp_path):
    socket = "aq-test-path-" + uuid.uuid4().hex

    class IsolatedProvider(TmuxProvider):
        async def _tmux(self, *args, **kwargs):
            # Do not load an operator's personal configuration on this server.
            return await super()._tmux("-f", "/dev/null", *args, **kwargs)

    provider = IsolatedProvider(
        SimpleNamespace(
            data_dir=str(tmp_path / "state"),
            sessions=SimpleNamespace(tmux_socket=socket),
        )
    )
    try:
        yield provider
    finally:
        with contextlib.suppress(TmuxCommandError):
            await provider._tmux("kill-server", timeout=10)
        tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
        Path(f"{tmpdir}/tmux-{os.getuid()}/{socket}").unlink(missing_ok=True)


def fake_executable(directory, identity):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "aq-path-stub"
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps({"
        f"'identity': {identity!r}, "
        "'path': os.environ.get('PATH'), "
        "'other_env': os.environ.get('AQ_PATH_TEST'), 'args': sys.argv[2:]}))\n"
        "print('READY', flush=True)\n"
        "time.sleep(30)\n"
    )
    path.chmod(0o755)
    return path


async def launch_and_observe(provider, tmp_path, command, env):
    # tmux 3.4 replaces a new pane's PATH with the client's PATH even if
    # new-session -e PATH=... populated the session metadata correctly.
    await provider._tmux("new-session", "-d", "-s", "keeper", "/bin/sleep 30")
    await provider._tmux("set-option", "-g", "default-shell", "/bin/sh")
    output = tmp_path / "observed.json"
    literal_arg = "literal '$PATH; $(touch argument-injected)"
    spec = SessionSpec(
        session_name="path-probe",
        work_dir=str(tmp_path),
        command=(command, str(output), literal_arg),
        env=env,
        ready_prompt_prefix="READY",
        process_names=("python",),
    )
    await provider.start(spec)
    for _ in range(50):
        if output.exists():
            break
        await asyncio.sleep(0.02)
    assert output.exists(), "temporary stub did not execute"
    assert not (tmp_path / "argument-injected").exists()
    return json.loads(output.read_text()), literal_arg


@pytest.mark.parametrize("path_kind", ["absolute", "relative", "literal-metacharacters"])
async def test_tmux_exec_uses_validated_session_path(
    isolated_tmux,
    tmp_path,
    monkeypatch,
    path_kind,
):
    ambient = tmp_path / "ambient"
    fake_executable(ambient, "daemon-path-executable")
    monkeypatch.setenv("PATH", str(ambient) + os.pathsep + os.defpath)
    directory = (
        "tools ' $(touch injected) $PATH `touch injected-too`"
        if path_kind == "literal-metacharacters"
        else "session tools"
    )
    binary = fake_executable(tmp_path / directory, "session-path-executable")
    session_path = directory if path_kind == "relative" else str(binary.parent)
    other_env = "literal '$HOME; $(touch env-injected)"
    observed, literal_arg = await launch_and_observe(
        isolated_tmux,
        tmp_path,
        binary.name,
        {"PATH": session_path, "AQ_PATH_TEST": other_env},
    )
    assert observed == {
        "identity": "session-path-executable",
        "path": session_path,
        "other_env": other_env,
        "args": [literal_arg],
    }
    for name in ("injected", "injected-too", "env-injected"):
        assert not (tmp_path / name).exists(), "PATH or environment was shell-expanded"


async def test_tmux_exec_uses_preflight_default_when_path_is_absent(
    isolated_tmux,
    tmp_path,
    monkeypatch,
):
    binary = fake_executable(tmp_path / "tools", "absolute-executable")
    monkeypatch.setenv("PATH", str(tmp_path / "ambient") + os.pathsep + os.defpath)
    observed, literal_arg = await launch_and_observe(
        isolated_tmux,
        tmp_path,
        str(binary),
        {"AQ_PATH_TEST": "retained"},
    )
    assert observed == {
        "identity": "absolute-executable",
        "path": os.defpath,
        "other_env": "retained",
        "args": [literal_arg],
    }
