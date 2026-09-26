"""Retained byte replay, bounded viewers, CLI detach and publisher adapters."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.api.auth import RequestScope
from src.api.dependencies import get_command_handler
from src.api.job_output import router
from src.api.streams import build_streams_router
from src.commands import CommandHandler
from src.config import AppConfig
from src.jobs.adapters import finite_command, validation_evidence
from src.jobs.artifacts import OutputStore, job_directory
from src.jobs.output import OutputHub, output_frames, read_output
from src.jobs.policy import JobError
from tests.test_jobs_queries import db as _db_fixture, values

db = _db_fixture


@pytest.fixture
async def output(db, tmp_path):
    config = AppConfig(data_dir=str(tmp_path / "data"))
    handler = CommandHandler(SimpleNamespace(db=db, plugin_registry=None), config)
    job = await db.submit_job(values(contract={"head_bytes": 8, "tail_bytes": 16}))
    directory = job_directory(Path(config.data_dir), job["id"])
    store = OutputStore(directory, head_bytes=8, tail_bytes=16)
    store.append(b"HEADabcd" + b"x" * 16 + b"LASTefghijklmnop")
    store.close()
    job = await db.transition_job(job["id"], 0, "starting")
    job = await db.transition_job(
        job["id"], 1, "failed", cleaned=True, output_retention="retained", result={"exit_code": 1}
    )
    return handler, job


def app_for(handler, scope=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_command_handler] = lambda: handler

    @app.middleware("http")
    async def inject(request: Request, call_next):
        request.state.scope = scope or RequestScope(kind="local")
        return await call_next(request)

    return app


def frames(response):
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


async def test_sse_gap_terminal_and_reconnect_after_reader_restart(output):
    handler, job = output
    async with AsyncClient(
        transport=ASGITransport(app=app_for(handler)), base_url="http://test"
    ) as c:
        response = await c.get(f"/api/jobs/{job['id']}/output")
        assert response.status_code == 200
        assert [f["type"] for f in frames(response)] == ["chunk", "gap", "chunk", "terminal"]
        assert frames(response)[1] == {"type": "gap", "after": 8, "next": 24}
        assert frames(response)[-1]["result"]["exit_code"] == 1
        # A daemon restart loses the viewer hub, not byte offsets or retained output.
        del handler._job_output_hub
        resumed = frames(await c.get(f"/api/jobs/{job['id']}/output?after=28"))
        assert resumed[0]["offset"] == 28 and resumed[0]["data"] == "efghijklmnop"
        assert resumed[-1]["next"] == 40
    assert handler._job_output_hub.attachments == {}


async def test_sse_missing_foreign_deleted_owner_and_expired_logs(output):
    handler, job = output
    async with AsyncClient(
        transport=ASGITransport(app=app_for(handler)), base_url="http://test"
    ) as c:
        assert (await c.get(f"/api/jobs/{uuid.uuid4()}/output")).status_code == 404
        await handler.db.expire_job_output(job["id"])
        expired = await c.get(f"/api/jobs/{job['id']}/output")
        assert expired.status_code == 410 and expired.json()["result"]["exit_code"] == 1
    # Unknown session identities never inherit trusted-local reader access.
    app = app_for(handler, RequestScope(kind="session", session_id="foreign", project_id="q"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get(f"/api/jobs/{job['id']}/output")).status_code == 404
    await handler.db.delete_task("t")
    async with AsyncClient(
        transport=ASGITransport(app=app_for(handler)), base_url="http://test"
    ) as c:
        assert (await c.get(f"/api/jobs/{job['id']}/output")).status_code == 404


async def test_subscriber_cap_and_one_reader_per_job():
    handler = SimpleNamespace(execute=AsyncMock(return_value={"success": False}))
    hub = OutputHub(handler)
    one, two = hub.subscribe("j", "p", 0), hub.subscribe("j", "p", 5)
    reader = hub.readers["j"]
    with pytest.raises(ValueError, match="attachment_limit"):
        hub.subscribe("j", "p", 0)
    other = hub.subscribe("j", "q", 0)
    assert hub.readers["j"] is reader
    for attachment in (one, two, other):
        hub.unsubscribe("j", attachment)
    await asyncio.gather(reader, return_exceptions=True)
    assert hub.attachments == {} and hub.readers == {}


async def test_live_attachment_gets_terminal_when_cancelled_before_spawn(db, tmp_path):
    handler = CommandHandler(
        SimpleNamespace(db=db, plugin_registry=None), AppConfig(data_dir=str(tmp_path / "data"))
    )
    job = await db.submit_job(values(contract={"head_bytes": 8, "tail_bytes": 16}))
    hub = OutputHub(handler, poll_seconds=0.001)
    attachment = hub.subscribe(job["id"], "viewer", 0)
    try:
        assert (await handler.execute("job_cancel", {"job_id": job["id"]}))["success"]
        frame = await asyncio.wait_for(attachment.queue.get(), 5)
        assert frame["type"] == "terminal" and frame["state"] == "cancelled"
        assert frame["result"]["outcome"] == "cancelled"
    finally:
        hub.unsubscribe(job["id"], attachment)


async def test_slow_viewer_disconnect_does_not_stall_live_output(tmp_path):
    job = {
        "id": str(uuid.uuid4()),
        "state": "running",
        "result": None,
        "contract": {"head_bytes": 1, "tail_bytes": 64},
    }
    store = OutputStore(job_directory(tmp_path, job["id"]), head_bytes=1, tail_bytes=64)

    async def execute(name, args):
        if name == "job_get":
            return {"success": True, "job": dict(job)}
        return {
            "success": True,
            **await asyncio.to_thread(read_output, tmp_path, job, args["after"], args["limit"]),
        }

    hub = OutputHub(SimpleNamespace(execute=execute), poll_seconds=0.001, queue_size=2)
    slow, fast = hub.subscribe(job["id"], "slow", 0), hub.subscribe(job["id"], "fast", 0)
    reader = hub.readers[job["id"]]
    try:
        for _ in range(8):
            store.append(b"abcdefgh")
            frame = await asyncio.wait_for(fast.queue.get(), 5)
            assert frame["type"] == "chunk"
        assert slow.disconnected and slow.queue.qsize() == 1
        assert slow.queue.get_nowait()["type"] == "disconnect"
        job.update(state="succeeded", result={"exit_code": 0})
        assert (await asyncio.wait_for(fast.queue.get(), 5))["type"] == "terminal"
        assert store.manifest["seen"] == 64
    finally:
        hub.unsubscribe(job["id"], slow)
        hub.unsubscribe(job["id"], fast)
        await asyncio.gather(reader, return_exceptions=True)
        store.close()


async def test_shared_page_respects_a_subscriber_inside_a_chunk(output):
    handler, job = output
    hub = OutputHub(handler)
    first, middle = hub.subscribe(job["id"], "a", 0), hub.subscribe(job["id"], "b", 28)
    try:
        received = []
        while not received or received[-1]["type"] != "terminal":
            received.append(await asyncio.wait_for(middle.queue.get(), 5))
        assert received[0]["data"] == "efghijklmnop"
        assert received[0]["offset"] == 28
    finally:
        hub.unsubscribe(job["id"], first)
        hub.unsubscribe(job["id"], middle)


async def test_attachment_arriving_during_final_read_replays_its_own_cursor(output):
    handler, job = output
    entered, release = asyncio.Event(), asyncio.Event()
    execute = handler.execute
    first_read = True

    async def pause_read(name, args):
        nonlocal first_read
        if name == "job_logs" and first_read:
            first_read = False
            entered.set()
            await release.wait()
        return await execute(name, args)

    hub = OutputHub(SimpleNamespace(execute=pause_read))
    first = hub.subscribe(job["id"], "a", 8)
    await asyncio.wait_for(entered.wait(), 5)
    late = hub.subscribe(job["id"], "b", 0)
    release.set()
    try:
        received = []
        while not received or received[-1]["type"] != "terminal":
            received.append(await asyncio.wait_for(late.queue.get(), 5))
        assert received[0]["offset"] == 0 and received[0]["data"] == "HEADabcd"
        assert received[-1]["next"] == 40
    finally:
        hub.unsubscribe(job["id"], first)
        hub.unsubscribe(job["id"], late)


def test_byte_cursors_survive_invalid_utf8_and_omitted_ranges(tmp_path):
    job = {"id": str(uuid.uuid4()), "contract": {"head_bytes": 8, "tail_bytes": 16}}
    store = OutputStore(job_directory(tmp_path, job["id"]), head_bytes=8, tail_bytes=16)
    store.append(b"\xff" + "🐈".encode())
    store.close()
    response = read_output(tmp_path, job, 0, 64)
    assert response["next"] == response["chunks"][0]["next"] == 5
    assert len(response["chunks"][0]["data"].encode()) != 5
    assert output_frames(response, 0)[0]["next"] == 5


def test_cli_follow_reports_gaps_and_ctrl_c_detaches(monkeypatch):
    from src.cli import jobs as cli_jobs
    from src.cli.app import cli

    calls = []

    @asynccontextmanager
    async def client(*args):
        async def stream(job_id, after):
            calls.append((job_id, after))
            yield {"type": "chunk", "data": "\x1b[31mhello", "next": 5}
            yield {"type": "gap", "after": 5, "next": 20}
            yield {"type": "terminal", "next": 20, "result": {"exit_code": 1}}

        yield SimpleNamespace(job_output=stream)

    monkeypatch.setattr(cli_jobs, "_get_client", client)
    result = CliRunner().invoke(cli, ["job", "logs", "j", "--follow", "--after", "2"])
    assert result.exit_code == 0 and "hello" in result.output
    assert "output omitted: bytes 5..20" in result.output and "\x1b" not in result.output
    assert calls == [("j", 2)]

    @asynccontextmanager
    async def interrupted(*args):
        async def stream(*args, **kwargs):
            raise KeyboardInterrupt()
            yield  # async generator

        yield SimpleNamespace(job_output=stream)

    monkeypatch.setattr(cli_jobs, "_get_client", interrupted)
    result = CliRunner().invoke(cli, ["job", "attach", "j"])
    assert "execution continues" in result.output and "aq job cancel j" in result.output


@pytest.mark.parametrize(
    "command",
    [
        "sleep 10",
        "bash -c pytest",
        "aq test tests/ && echo ok",
        "npm run dev",
        "python -m http.server",
    ],
)
def test_finite_adapter_refuses_independent_shell_or_watchers(command):
    with pytest.raises(JobError):
        finite_command(command)


def test_publisher_evidence_keeps_exit_five_failure_and_requires_verified_snapshot():
    policy = SimpleNamespace(timeout_seconds=300, slot_wait_seconds=600)
    job = {
        "id": "j",
        "input_ref": "sha",
        "result": {
            "outcome": "failed",
            "exit_code": 5,
            "input_stability": "stable",
            "input_ref": "sha",
            "result_hash": "hash",
            "queue_seconds": 8,
            "run_seconds": 2,
        },
    }
    check = validation_evidence("aq test tests/", job, policy)
    assert check["outcome"] == "failed" and check["exit_code"] == 5
    assert check["duration_seconds"] == 10 and check["input_ref"] == "sha"
    job["result"].update(outcome="passed", exit_code=0, input_stability="unverified")
    assert validation_evidence("aq test tests/", job, policy)["outcome"] == "infrastructure"


async def test_managed_console_stream_replays_retained_ranges_after_registry_loss(output):
    handler, job = output
    app = app_for(handler)
    app.include_router(
        build_streams_router(
            db=handler.db, config=handler.config, workspace_dir="/unused", handler=handler
        )
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get(f"/api/streams/{job['id']}/subscribe?after_seq=7")
        replayed = frames(response)
        assert replayed[0]["truncated"] and "bytes 8..24" in replayed[0]["text"]
        assert replayed[1]["seq"] == 39 and replayed[-1]["type"] == "exit"
        assert (await c.get(f"/api/streams/{job['id']}")).json()["status"] == "exited"


async def test_publisher_queue_runs_snapshot_replays_result_and_removes_pin(db, tmp_path):
    from src.git.manager import GitManager
    from src.jobs.publisher import publisher_check
    from src.jobs.service import JobService

    source = tmp_path / "source"
    source.mkdir()
    (source / "example.py").write_text("answer = 42\n")
    (source / ".gitignore").write_text(".ruff_cache/\n")
    git = GitManager()
    for args in (
        ["init"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@test", "commit", "-m", "fixture"],
    ):
        await git._arun(args, cwd=str(source))
    head = (await git._arun(["rev-parse", "HEAD"], cwd=str(source))).strip()
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    service = JobService(db, config)
    policy = SimpleNamespace(timeout_seconds=30, slot_wait_seconds=30)
    check = await asyncio.wait_for(
        publisher_check(service, git, source, "p", head, "ruff check example.py", policy),
        60,
    )
    assert check["outcome"] == "passed", check
    assert check["input_ref"] == head and check["input_stability"] == "stable"
    job = await db.get_job(check["job_id"])
    assert job["owner_kind"] == "integration" and job["priority_band"] == 1
    assert not await db.workspace_has_job_pin(job["workspace_id"])
    assert await db.get_workspace(job["workspace_id"]) is None
    assert not Path(job["contract"]["cwd"]).exists()
    replay = await publisher_check(service, git, source, "p", head, "ruff check example.py", policy)
    assert replay["job_id"] == check["job_id"] and replay["result_hash"] == check["result_hash"]
    assert len(await db.list_jobs(project_id="p")) == 1


async def test_interrupted_publisher_leaves_pin_and_resumes_the_same_producer(db, tmp_path):
    from src.git.manager import GitManager
    from src.jobs.publisher import publisher_check
    from src.jobs.service import JobService

    source = tmp_path / "source"
    source.mkdir()
    (source / "example.py").write_text("answer = 42\n")
    (source / ".gitignore").write_text(".ruff_cache/\n")
    git = GitManager()
    for args in (
        ["init"],
        ["add", "."],
        ["-c", "user.name=Test", "-c", "user.email=test@test", "commit", "-m", "fixture"],
    ):
        await git._arun(args, cwd=str(source))
    head = (await git._arun(["rev-parse", "HEAD"], cwd=str(source))).strip()
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    service = JobService(db, config)
    tick = service.tick
    service.tick = AsyncMock(side_effect=asyncio.CancelledError())
    policy = SimpleNamespace(timeout_seconds=30, slot_wait_seconds=30)
    with pytest.raises(asyncio.CancelledError):
        await publisher_check(service, git, source, "p", head, "ruff check example.py", policy)
    job = (await db.list_jobs(project_id="p"))[0]
    assert await db.workspace_has_job_pin(job["workspace_id"])
    workspace = await db.get_workspace(job["workspace_id"])
    assert not workspace.enabled and workspace.kind_id == "integration-snapshot"
    assert await db.acquire_workspace("p", "a", "t") is None
    service.tick = tick
    check = await asyncio.wait_for(
        publisher_check(service, git, source, "p", head, "ruff check example.py", policy),
        60,
    )
    assert check["outcome"] == "passed" and check["job_id"] == job["id"]
    assert len(await db.list_jobs(project_id="p")) == 1


def test_console_adapter_uses_original_bytes_and_bounds_giant_rows():
    import base64
    from src.api.job_streams import console_frames

    raw = b"\xff\n" + "🐈\n".encode() + b"x" * 20000
    frames = console_frames(
        {
            "type": "chunk",
            "offset": 10,
            "next": 10 + len(raw),
            "data_base64": base64.b64encode(raw).decode(),
            "data": raw.decode("utf-8", "replace"),
        }
    )
    assert [frame["seq"] for frame in frames[:2]] == [11, 16]
    assert frames[-1]["seq"] == 10 + len(raw) - 1
    assert max(len(frame["text"]) for frame in frames) <= 4096
