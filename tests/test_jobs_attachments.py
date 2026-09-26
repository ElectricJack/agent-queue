"""Retained byte cursors, CLI follow/attach and finite command adapters."""

from __future__ import annotations

import base64
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.api.auth import RequestScope
from src.commands import CommandHandler
from src.config import AppConfig
from src.jobs.adapters import finite_command
from src.jobs.artifacts import OutputStore, job_directory
from src.jobs.output import read_output
from src.jobs.policy import JobError
from tests.test_jobs_queries import db as _db_fixture
from tests.test_jobs_queries import values

db = _db_fixture


@pytest.fixture
async def output(db, tmp_path):
    config = AppConfig(data_dir=str(tmp_path / "data"))
    handler = CommandHandler(SimpleNamespace(db=db, plugin_registry=None), config)
    job = await db.submit_job(
        values(contract={"head_bytes": 8, "tail_bytes": 16, "cwd": str(tmp_path)})
    )
    directory = job_directory(Path(config.data_dir), job["id"])
    store = OutputStore(directory, head_bytes=8, tail_bytes=16)
    store.append(b"HEADabcd" + b"x" * 16 + b"LASTefghijklmnop")
    store.close()
    job = await db.transition_job(job["id"], 0, "starting")
    job = await db.transition_job(
        job["id"], 1, "failed", cleaned=True, output_retention="retained", result={"exit_code": 1}
    )
    return handler, job


def test_byte_cursors_survive_invalid_utf8(tmp_path):
    job = {"id": str(uuid.uuid4()), "contract": {"head_bytes": 8, "tail_bytes": 16}}
    raw = b"\xff" + "🐈".encode()
    store = OutputStore(job_directory(tmp_path, job["id"]), head_bytes=8, tail_bytes=16)
    store.append(raw)
    store.close()
    response = read_output(tmp_path, job, 0, 64)
    chunk = response["chunks"][0]
    assert response["next"] == chunk["next"] == 5
    assert len(chunk["data"].encode()) != 5
    assert base64.b64decode(chunk["data_base64"]) == raw


async def test_job_logs_reports_byte_cursors_and_explicit_gaps(output):
    handler, job = output
    response = await handler.execute("job_logs", {"job_id": job["id"], "after": 0})
    assert response["success"], response
    first = response["chunks"][0]
    assert (first["offset"], first["next"], first["data"]) == (0, 8, "HEADabcd")
    assert [(g["after"], g["next"]) for g in response["gaps"]] == [(8, 24)]
    assert response["chunks"][-1]["next"] == response["next"] == response["seen"] == 40
    resumed = await handler.execute("job_logs", {"job_id": job["id"], "after": 28})
    assert resumed["chunks"][0]["offset"] == 28
    assert base64.b64decode(resumed["chunks"][0]["data_base64"]) == b"efghijklmnop"


async def test_cli_client_reads_the_job_output_viewer(output, monkeypatch):
    """The CLI consumes the same SSE viewer the console pane uses."""
    from src.api import dependencies
    from src.api.streams import StreamRegistry, router
    from src.cli.client import CLIClient
    from src.cli.exceptions import CommandError

    handler, job = output
    monkeypatch.setattr(dependencies, "_orchestrator", SimpleNamespace(
        db=handler.db, config=handler.config, stream_registry=StreamRegistry(),
        _command_handler=handler,
    ))
    monkeypatch.setattr(dependencies, "_command_handler", handler)
    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def scope(request: Request, call_next):
        request.state.scope = RequestScope(kind="local")
        return await call_next(request)

    client = CLIClient(base_url="http://test")
    client._http = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        frames = [frame async for frame in client.job_output(job["id"], after=0)]
        assert frames[0]["type"] == "line" and frames[0]["text"] == "HEADabcd"
        assert [(f["after"], f["next"]) for f in frames if f["type"] == "gap"] == [(8, 24)]
        assert frames[-1]["type"] == "exit"
        assert max(f.get("next", 0) for f in frames) == 40
        resumed = [frame async for frame in client.job_output(job["id"], after=28)]
        assert resumed[0]["text"] == "efghijklmnop" and resumed[-1]["type"] == "exit"
        await handler.db.expire_job_output(job["id"])
        with pytest.raises(CommandError, match="logs_expired"):
            [frame async for frame in client.job_output(job["id"])]
    finally:
        await client._http.aclose()


def _fake_client(*connections):
    calls = []

    @asynccontextmanager
    async def client(*args):
        async def job_output(job_id, after):
            calls.append((job_id, after))
            for frame in connections[len(calls) - 1]:
                if isinstance(frame, BaseException):
                    raise frame
                yield frame

        yield SimpleNamespace(job_output=job_output)

    return client, calls


def test_cli_follow_reports_gaps_and_stops_at_terminal(monkeypatch):
    from src.cli import jobs as cli_jobs
    from src.cli.app import cli

    client, calls = _fake_client([
        {"type": "line", "seq": 7, "text": "\x1b[31mhello", "after": 2, "next": 7},
        {"type": "gap", "seq": 20, "after": 7, "next": 20, "text": "[output gap: 7..20]"},
        {"type": "exit", "seq": 21, "rc": 1},
        {"type": "line", "seq": 30, "text": "after terminal", "after": 20, "next": 30},
    ])
    monkeypatch.setattr(cli_jobs, "_get_client", client)
    result = CliRunner().invoke(cli, ["job", "logs", "j", "--follow", "--after", "2"])
    assert result.exit_code == 0, result.output
    assert "hello" in result.output and "\x1b" not in result.output
    assert "output omitted: bytes 7..20" in result.output
    assert "after terminal" not in result.output
    assert calls == [("j", 2)]


def test_cli_slow_reader_reconnects_from_its_delivered_cursor(monkeypatch):
    from src.cli import jobs as cli_jobs
    from src.cli.app import cli

    marker = "slow reader disconnected; reconnect to resume"
    client, calls = _fake_client(
        [
            {"type": "line", "seq": 12, "text": "one", "after": 0, "next": 12},
            # The server may name an older queued cursor; nothing was omitted.
            {"type": "gap", "seq": 4, "after": 4, "next": 4, "text": marker},
        ],
        [
            {"type": "line", "seq": 20, "text": "two", "after": 12, "next": 20},
            {"type": "exit", "seq": 21, "rc": 0},
        ],
    )
    monkeypatch.setattr(cli_jobs, "_get_client", client)
    result = CliRunner().invoke(cli, ["job", "attach", "j"])
    assert result.exit_code == 0, result.output
    assert calls == [("j", 0), ("j", 12)]
    assert "one" in result.output and "two" in result.output
    assert "omitted" not in result.output


def test_cli_unexpected_end_names_resume_cursor_and_ctrl_c_detaches(monkeypatch):
    from src.cli import jobs as cli_jobs
    from src.cli.app import cli

    client, _ = _fake_client([{"type": "line", "seq": 9, "text": "x", "after": 0, "next": 9}])
    monkeypatch.setattr(cli_jobs, "_get_client", client)
    result = CliRunner().invoke(cli, ["job", "attach", "j"])
    assert result.exit_code != 0
    assert "aq job logs j --follow --after 9" in result.output

    client, _ = _fake_client([KeyboardInterrupt()])
    monkeypatch.setattr(cli_jobs, "_get_client", client)
    result = CliRunner().invoke(cli, ["job", "attach", "j"])
    assert "execution continues" in result.output and "aq job cancel j" in result.output


@pytest.mark.parametrize(
    "command",
    [
        "sleep 10",
        "bash -c pytest",
        "aq test tests/ && echo ok",
        "aq test tests/ >> out.log",
        "npm run dev",
        "python -m http.server",
    ],
)
def test_finite_adapter_refuses_independent_shell_or_watchers(command):
    with pytest.raises(JobError):
        finite_command(command)


def test_finite_adapter_accepts_python3_ruff():
    assert finite_command("python3 -m ruff check src") == ("lint", ["src"])
