"""Real providers reject missing executables before spawning or writing files."""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.sessions.provider import SessionDiedDuringStartup, SessionError, SessionSpec
from src.sessions.subprocess import SubprocessProvider
from src.sessions.tmux import TmuxProvider


@pytest.mark.parametrize("provider_type", [TmuxProvider, SubprocessProvider])
async def test_missing_executable_fails_before_any_spawn_or_file_write(
    tmp_path,
    monkeypatch,
    provider_type,
):
    async def forbidden_spawn(*args, **kwargs):
        raise AssertionError("No process may be started during preflight")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    work_dir = tmp_path / "work"
    spec = SessionSpec(
        session_name="n-preflight",
        work_dir=str(work_dir),
        command=("missing-agent-cli", "secret-command-argument"),
        env={"PATH": str(tmp_path / "empty"), "AQ_API_TOKEN": "secret-token"},
        files=((".aq/hooks/test.json", "{}"),),
    )
    with pytest.raises(SessionError) as caught:
        await provider_type().start(spec)
    error = str(caught.value)
    assert "missing-agent-cli" in error and "PATH" in error
    assert "secret-command-argument" not in error and "secret-token" not in error
    assert str(tmp_path) not in error
    assert not work_dir.exists()


def _validate(spec):
    from src.sessions import provider

    validate = getattr(provider, "require_session_executable", None)
    assert validate is not None, "real providers need a shared executable preflight"
    return validate(spec)


def test_preflight_uses_session_path_not_daemon_path(tmp_path, monkeypatch):
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    executable = ambient / "only-in-daemon-path"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(ambient))
    spec = SessionSpec(
        session_name="n-preflight",
        work_dir=str(tmp_path),
        command=(executable.name,),
        env={"PATH": str(tmp_path / "empty")},
    )
    with pytest.raises(SessionError):
        _validate(spec)
    spec.env["PATH"] = str(ambient)
    _validate(spec)


def test_preflight_resolves_relative_executable_and_path_from_work_dir(tmp_path):
    work = tmp_path / "work"
    binary_dir = work / "bin"
    binary_dir.mkdir(parents=True)
    executable = binary_dir / "custom-agent"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    _validate(
        SessionSpec(
            session_name="n-preflight",
            work_dir=str(work),
            command=("./bin/custom-agent",),
            env={},
        )
    )
    _validate(
        SessionSpec(
            session_name="n-preflight",
            work_dir=str(work),
            command=("custom-agent",),
            env={"PATH": "bin"},
        )
    )
    executable.chmod(0o644)
    with pytest.raises(SessionError):
        _validate(
            SessionSpec(
                session_name="n-preflight",
                work_dir=str(work),
                command=(str(executable),),
                env={"PATH": os.defpath},
            )
        )


async def test_tmux_exec_error_survives_disappearing_pane(tmp_path):
    # Executable preflight passes, but the interpreter is missing at exec time.
    executable = tmp_path / "broken-harness"
    executable.write_text("#!/nonexistent/aq-test-interpreter\n")
    executable.chmod(0o755)
    provider = TmuxProvider(SimpleNamespace(data_dir=str(tmp_path / "state")))

    async def scripted_tmux(*args, **kwargs):
        if args[0] == "new-session":
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", args[-1], cwd=tmp_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            assert proc.returncode == 127
        if args[0] in {"capture-pane", "list-panes"}:
            from src.sessions.tmux import TmuxCommandError

            raise TmuxCommandError(args, 1, "pane disappeared")
        return ""

    provider._tmux = scripted_tmux
    spec = SessionSpec(session_name="broken", work_dir=str(tmp_path),
                       command=(str(executable), "secret-argument"),
                       env={"AQ_API_TOKEN": "secret-token"})
    with pytest.raises(SessionDiedDuringStartup, match="ENOENT") as caught:
        await provider.start(spec)
    diagnostic = Path(caught.value.start_stderr_path).read_text()
    assert "Harness exec failed: ENOENT" in diagnostic
    assert "Startup failed: process died before start" in diagnostic
    assert "secret" not in diagnostic


async def test_tmux_exec_reports_e2big_without_logging_arguments(tmp_path):
    from src.sessions.tmux import _EXEC_SCRIPT

    log_path = tmp_path / "start-stderr.log"
    # Grow the argument in the launcher so its *next* exec hits Linux's limit.
    script = (
        "import sys; sys.argv = [sys.argv[0], sys.argv[1], '/bin/true', "
        "'secret-argument' * 20_000]\n" + _EXEC_SCRIPT
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(log_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    assert proc.returncode == 127
    assert "E2BIG" in stderr.decode()
    diagnostic = log_path.read_text()
    assert "argument or environment too large" in diagnostic
    assert "secret-argument" not in diagnostic


async def test_subprocess_creation_error_is_written_to_log(tmp_path):
    spec = SessionSpec(session_name="too-large", work_dir=str(tmp_path),
                       command=(sys.executable, "secret-argument" * 20_000))
    provider = SubprocessProvider(SimpleNamespace(data_dir=str(tmp_path / "state")))
    with pytest.raises(SessionDiedDuringStartup) as caught:
        await provider.start(spec)
    diagnostic = Path(caught.value.start_stderr_path).read_text()
    assert "Process creation failed" in diagnostic
    assert "Argument list too long" in diagnostic
    assert "secret-argument" not in diagnostic
