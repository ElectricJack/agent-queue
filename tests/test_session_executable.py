"""Real providers reject missing executables before spawning or writing files."""

import asyncio
import os

import pytest

from src.sessions.provider import SessionError, SessionSpec
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
