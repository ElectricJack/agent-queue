"""Console viewers submit finite jobs and never own process lifetime."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.api.auth import RequestScope
from src.api.streams import StreamRegistry, build_streams_router, _view_job
from src.commands import CommandHandler
from src.config import AppConfig
from src.jobs.artifacts import OutputStore, job_directory
from src.jobs.policy import Preset
from src.models import Workspace, RepoSourceType
from tests.test_agent_wait_queries import env as env


@pytest.fixture
async def setup(env, tmp_path, monkeypatch):
    await env.db.create_workspace(Workspace(
        id="w", project_id="p", workspace_path=str(tmp_path), source_type=RepoSourceType.LINK,
        locked_by_agent_id="a", locked_by_task_id="owner",
    ))
    config = AppConfig(data_dir=str(tmp_path / "data"))
    config.resources.jobs.enabled = True
    config.streams.client_reconnect_attempts = 7
    monkeypatch.setattr("src.jobs.service.presets", lambda root: {
        "lint": Preset("lint", (sys.executable, "-c", "print('hello from job')")),
    })
    orch = SimpleNamespace(db=env.db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None)
    handler = CommandHandler(orch, config)
    return SimpleNamespace(db=env.db, config=config, handler=handler, cwd=tmp_path,
                           registry=StreamRegistry(), scope=RequestScope(kind="local"))


def app_for(env, *, registry=None):
    app = FastAPI()
    app.include_router(build_streams_router(
        db=env.db, config=env.config, workspace_dir=str(env.cwd), command_handler=env.handler,
        registry=registry if registry is not None else env.registry,
    ))

    @app.middleware("http")
    async def scope(request: Request, call_next):
        request.state.scope = env.scope
        return await call_next(request)
    return app


def client_for(env, *, registry=None):
    return AsyncClient(transport=ASGITransport(app=app_for(env, registry=registry)),
                       base_url="http://test")


async def start(client, env, **overrides):
    return await client.post("/api/streams", json={
        "command": ["ruff", "check", "src"], "cwd": str(env.cwd), "session_id": "s",
        "project_id": "p", "idempotency_key": "stream-key", **overrides,
    })


async def finish(env, job_id):
    async def wait():
        while True:
            await env.handler._cmd_job_reconcile({})
            job = await env.db.get_job(job_id)
            if job["state"] in {"succeeded", "failed", "cancelled", "lost"}:
                return job
            await asyncio.sleep(0.02)
    return await asyncio.wait_for(wait(), 20)


async def test_start_is_idempotent_and_stream_id_is_job_id(setup):
    async with client_for(setup) as client:
        first = await start(client, setup)
        second = await start(client, setup)
    assert first.status_code == second.status_code == 200
    assert first.json()["stream_id"] == second.json()["stream_id"]
    jobs = await setup.db.list_jobs(project_id="p")
    assert len(jobs) == 1 and jobs[0]["id"] == first.json()["stream_id"]
    assert jobs[0]["submitter_session_id"] == "s"
    assert await setup.db.workspace_has_job_pin("w")
    assert not hasattr(setup.registry.get(jobs[0]["id"]), "process")
    await finish(setup, jobs[0]["id"])


@pytest.mark.parametrize("command", ["echo hi", None, {}, [], ["sleep", "30"], ["bash", "-c", "true"]])
async def test_arbitrary_commands_do_not_spawn_or_create_jobs(setup, monkeypatch, command):
    spawn = AsyncMock(side_effect=AssertionError("adapter spawned a process"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    async with client_for(setup) as client:
        response = await start(client, setup, command=command)
    assert response.status_code == 400
    assert await setup.db.list_jobs(project_id="p") == []
    spawn.assert_not_awaited()


@pytest.mark.parametrize("overrides,code", [
    ({"cwd": "/"}, 403), ({"project_id": "q"}, 403),
    ({"session_id": "absent"}, 400), ({"idempotency_key": None}, 400),
])
async def test_start_requires_owned_workspace_project_and_replay_key(setup, overrides, code):
    async with client_for(setup) as client:
        response = await start(client, setup, **overrides)
    assert response.status_code == code
    assert await setup.db.list_jobs(project_id="p") == []


async def test_feature_off_refuses_instead_of_falling_back(setup):
    setup.config.resources.jobs.enabled = False
    async with client_for(setup) as client:
        response = await start(client, setup)
    assert response.status_code == 503
    assert await setup.db.list_jobs(project_id="p") == []


async def test_restart_reconstructs_viewer_and_replays_output(setup):
    async with client_for(setup) as client:
        job_id = (await start(client, setup)).json()["stream_id"]
    await finish(setup, job_id)
    setup.registry.evict(job_id)
    restored = StreamRegistry()
    async with client_for(setup, registry=restored) as client:
        response = await client.get(f"/api/streams/{job_id}/subscribe")
        metadata = await client.get(f"/api/streams/{job_id}")
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert "hello from job" in "".join(f.get("text", "") for f in frames)
    assert frames[-1]["type"] == "exit" and frames[-1]["rc"] == 0
    assert metadata.json()["status"] == "exited"
    assert metadata.json()["client_reconnect_attempts"] == 7
    assert not await setup.db.workspace_has_job_pin("w")


async def test_foreign_task_cannot_read_or_cancel_cached_or_durable_job(setup):
    async with client_for(setup) as client:
        job_id = (await start(client, setup)).json()["stream_id"]
        setup.scope = RequestScope(kind="session", session_id="other", task_id="foreign", project_id="q")
        for suffix, method in [("", "get"), ("/tail", "get"), ("/subscribe", "get"), ("/kill", "post")]:
            assert (await getattr(client, method)(f"/api/streams/{job_id}{suffix}")).status_code == 404
    assert (await setup.db.get_job(job_id))["state"] == "queued"
    setup.scope = RequestScope(kind="local")
    await finish(setup, job_id)


async def test_cancel_uses_job_cleanup_and_is_idempotent(setup):
    async with client_for(setup) as client:
        job_id = (await start(client, setup)).json()["stream_id"]
        response = await client.post(f"/api/streams/{job_id}/kill")
        assert response.status_code == 200
        assert (await setup.db.get_job(job_id))["state"] == "cancelled"
        await asyncio.sleep(0.3)
        response = await client.post(f"/api/streams/{job_id}/kill")
        assert response.json()["status"] == "killed" or response.status_code == 410
    assert not await setup.db.workspace_has_job_pin("w")


async def test_viewer_eviction_preserves_running_job_and_pin(setup, monkeypatch):
    marker = setup.cwd / "started"
    script = f"import pathlib,time; pathlib.Path({str(marker)!r}).write_text('yes'); time.sleep(30)"
    monkeypatch.setattr("src.jobs.service.presets", lambda root: {
        "lint": Preset("lint", (sys.executable, "-c", script)),
    })
    async with client_for(setup) as client:
        job_id = (await start(client, setup)).json()["stream_id"]
    await setup.handler._cmd_job_reconcile({})
    async def started():
        while not marker.exists():
            await asyncio.sleep(0.02)
    await asyncio.wait_for(started(), 15)
    setup.registry.evict(job_id)
    assert await setup.db.workspace_has_job_pin("w")
    assert (await setup.db.get_job(job_id))["state"] not in {"cancelled", "failed", "lost"}
    await setup.handler._cmd_job_cancel({"job_id": job_id})
    job = await finish(setup, job_id)
    assert job["state"] == "cancelled" and not await setup.db.workspace_has_job_pin("w")


async def test_retained_ranges_emit_explicit_gaps_and_terminal_frame(setup):
    setup.config.resources.jobs.head_bytes = 8
    setup.config.resources.jobs.tail_bytes = 16
    async with client_for(setup) as client:
        job_id = (await start(client, setup)).json()["stream_id"]
    await finish(setup, job_id)
    setup.registry.evict(job_id)
    store = OutputStore(job_directory(Path(setup.config.data_dir), job_id), head_bytes=8, tail_bytes=16)
    store.append(b"z" * 100)
    store.close()
    reg = StreamRegistry()
    handle = reg.create(title="gaps", session_id="s", project_id="p", command=[], cwd=str(setup.cwd), job_id=job_id)
    await _view_job(handle, reg, db=setup.db, config=setup.config)
    gap = next(f.to_dict() for f in handle.buffer if f.type == "gap")
    assert gap["next"] > gap["after"] and handle.truncated
    assert list(handle.buffer)[-1].type == "exit"


async def test_unknown_and_expired_logs_are_distinct(setup):
    from sqlalchemy import update
    from src.database.tables import jobs
    async with client_for(setup) as client:
        assert (await client.get("/api/streams/absent")).status_code == 404
        job_id = (await start(client, setup)).json()["stream_id"]
        job = await finish(setup, job_id)
        async with setup.db._engine.begin() as conn:
            await conn.execute(update(jobs).where(jobs.c.id == job_id).values(output_retention="expired"))
        response = await client.get(f"/api/streams/{job_id}/tail")
    assert response.status_code == 410
    assert response.json()["detail"]["result"] == job["result"]


async def test_job_output_endpoint_resumes_from_retained_offset_after_cache_eviction(setup, monkeypatch):
    from src.api import dependencies
    from src.api.streams import router
    async with client_for(setup) as client:
        job_id = (await start(client, setup)).json()["stream_id"]
    await finish(setup, job_id)
    setup.registry.evict(job_id)
    monkeypatch.setattr(dependencies, "_orchestrator", SimpleNamespace(
        db=setup.db, config=setup.config, stream_registry=StreamRegistry(buffer_max_lines=1),
        _command_handler=setup.handler,
    ))
    monkeypatch.setattr(dependencies, "_command_handler", setup.handler)
    app = app_for(setup)
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/jobs/{job_id}/output?after=0")
        frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        line = next(f for f in frames if f["type"] == "line")
        assert line["text"] == "hello from job"
        response = await client.get(f"/api/jobs/{job_id}/output?after={line['next']}")
        assert '"type": "line"' not in response.text
        assert '"type": "exit"' in response.text
        assert (await client.get(f"/api/jobs/{job_id}/output?after=-1")).status_code == 400
