"""Warm candidates and fixers keep durable ownership and job measurement fences."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, update

from src.build_cache import cache_environment
from src.config import BuildCacheConfig
from src.database.tables import integration_batches, jobs, workspaces
from src.integration.batches import Batch, BatchMember, BatchStore
from src.jobs.workspace import candidate_workspace
from src.models import RepoConfig, RepoSourceType
from src.waits.measurements import cost_values
from tests.test_integration_fixers import db as _db, live_fixer as _live_fixer
from tests.test_jobs import finish, jobs_handler, world as _world

db, live_fixer, world = _db, _live_fixer, _world


def request(world, batch="one", group="attempt"):
    return {"project_id": "p", "repository_id": "r", "operation_id": batch,
            "store": str(world.store), "input_ref": world.candidate, "snapshot_group": group}


async def freeze(world, batch):
    await BatchStore(world.db).freeze(
        Batch(id=batch, project_id="p", repository_id="r", target_ref="refs/heads/main"),
        (BatchMember(task_id="member", source_sha=world.candidate,
                     source_base_sha=world.candidate),),
        trees={"member": await world.git.arev_parse(str(world.store), world.candidate + "^{tree}")},
    )


async def setup_candidate(world):
    await world.db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.LINK,
                                         source_path=str(world.store)))
    await freeze(world, "one")
    # Ignored builds are the only output a warm pool may preserve on reset.
    (world.store / ".git" / "info" / "exclude").write_text("build/\n")
    async with candidate_workspace(world.db, world.config, request(world), "a") as wid:
        row = await world.db.get_workspace(wid)
    path = Path(row.workspace_path)
    (path / "build").mkdir()
    (path / "build" / "output.o").write_text("warm")
    return wid, path


async def test_candidate_reuses_across_released_batches_and_shares_objects(world):
    wid, path = await setup_candidate(world)
    async with world.db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "one",
        ).values(lifecycle="promoted"))
    await freeze(world, "two")
    async with candidate_workspace(world.db, world.config, request(world, "two"), "b") as other:
        assert other == wid
    assert (path / "build" / "output.o").read_text() == "warm"
    common = await world.git._arun(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=str(path),
    )
    assert common.strip() == str(world.store / ".git")


@pytest.mark.parametrize("retained", ["open", "dirty", "enabled", "pin", "unknown"])
async def test_candidates_preserve_unreleased_or_uncertain_work(world, retained):
    wid, path = await setup_candidate(world)
    if retained != "open":
        async with world.db.immediate() as conn:
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == "one",
            ).values(lifecycle="promoted"))
    if retained == "dirty":
        (path / "sample.py").write_text("uncommitted")
    if retained == "enabled":
        async with world.db.immediate() as conn:
            await conn.execute(update(workspaces).where(workspaces.c.id == wid).values(enabled=True))
    if retained == "pin":
        handler = jobs_handler(world.db, Path(world.config.data_dir).parent)
        await handler.service.submit(
            project_id="p", task_id=None, session_id=None, claim_epoch=None,
            workspace_id=wid, generation=0, owner_kind="integration", owner_id="one",
            input_mode="snapshot", input_ref=world.candidate, trusted_band=0,
            preset="lint", args=["sample.py"], idempotency_key="held",
        )
    if retained == "unknown":
        # Missing release evidence is not permission to adopt an old checkout.
        async with world.db.immediate() as conn:
            await conn.execute(delete(integration_batches).where(
                integration_batches.c.id == "one",
            ))
    await freeze(world, "two")
    async with candidate_workspace(world.db, world.config, request(world, "two"), "b") as other:
        assert other != wid
    assert (path / "build" / "output.o").read_text() == "warm"
    if retained == "dirty":
        assert (path / "sample.py").read_text() == "uncommitted"


async def test_cache_setting_reaches_real_job_contract_and_common_wait_sample(world):
    wid, path = await setup_candidate(world)
    handler = jobs_handler(world.db, Path(world.config.data_dir).parent)
    handler.config.workspaces.build_cache = BuildCacheConfig(kind="sccache", dir="/native/cache")
    job = await handler.service.submit(
        project_id="p", task_id=None, session_id=None, claim_epoch=None,
        workspace_id=wid, generation=0, owner_kind="integration", owner_id="one",
        input_mode="snapshot", input_ref=world.candidate, trusted_band=0,
        preset="lint", args=["sample.py"], idempotency_key="measured",
    )
    assert job["contract"]["env"]["SCCACHE_DIR"] == "/native/cache"
    assert job["contract"]["build_cache"] == {"kind": "sccache", "dir": "/native/cache"}
    job = await finish(world.db, job, exit_code=0)
    measured = {"rebuild_seconds": 2.5, "cache_hit_rate": .75, "workspace_path": str(path)}
    async with world.db.immediate() as conn:
        await conn.execute(update(jobs).where(jobs.c.id == job["id"]).values(
            result={**job["result"], "build_measurements": measured},
        ))
        assert await cost_values(conn, {"condition": {"kind": "job", "job_id": job["id"]}},
                                 None) == measured
        assert await cost_values(conn, {"condition": {"kind": "job", "job_id": "missing"}},
                                 None) == {}


async def test_fixer_reuse_requires_stopped_process_terminal_batch_and_saved_head(db, live_fixer):
    _, runtime, orch, row, _, source, _ = live_fixer
    state = (await db.get_integration_batch("real"))["fixer_state"]
    manifest = {"owner": {"session_id": row.id, "instance_token": row.instance_token},
                "oid": source}
    path = Path(row.work_dir)
    assert not await runtime._checkout_released(manifest, path, source)
    await db.update_session(row.id, state="stopped", desired_state="stopped")
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "real",
        ).values(lifecycle="promoted"))
    provider = orch.session_providers.create(row.provider)
    assert not await runtime._checkout_released(manifest, path, source)
    provider.is_running = AsyncMock(return_value=False)
    assert await runtime._checkout_released(manifest, path, source)
    assert not await runtime._checkout_released(manifest, path, "unpublished")
    config = orch.config
    config.workspaces.build_cache = BuildCacheConfig(dir="/native/fixer-cache")
    # Uses the same environment path as the real named-session builder.
    assert cache_environment(config, "p") == {"CCACHE_DIR": "/native/fixer-cache"}
    async with db._engine.connect() as conn:
        assert (await conn.execute(select(integration_batches.c.fixer_state).where(
            integration_batches.c.id == "real",
        ))).scalar_one()["work_dir"] == state["work_dir"]


async def test_next_fixer_batch_reuses_stopped_checkout_with_build_output(db, live_fixer):
    service, runtime, orch, row, base, source, _ = live_fixer
    path = Path(row.work_dir)
    await runtime.git._arun(["config", "core.excludesFile", str(path.parent / "ignored")],
                            cwd=str(path))
    (path.parent / "ignored").write_text("build/\n")
    (path / "build").mkdir()
    (path / "build" / "output.o").write_text("cached")
    await db.update_session(row.id, state="stopped", desired_state="stopped")
    async with db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "real",
        ).values(lifecycle="promoted"))
    orch.session_providers.create(row.provider).is_running = AsyncMock(return_value=False)
    await BatchStore(db).freeze(
        Batch(id="next", project_id="p", repository_id="r", target_ref="refs/heads/main"),
        (BatchMember(task_id="member-next", source_sha=source, source_base_sha=base),),
        trees={"member-next": await runtime.git.arev_parse(str(path), source + "^{tree}")},
    )
    from src.integration.batches import candidate_ref

    orch.config.workspaces.build_cache = BuildCacheConfig(dir="/native/fixer-cache")
    result = await service.allocate("next", target_ref=candidate_ref("next"), head_sha=source,
                                    authorize=AsyncMock(return_value=True))
    assert result["outcome"] == "filed", result
    next_session = await db.get_session(result["session_id"])
    provider = orch.session_providers.create(next_session.provider)
    assert provider.sessions[next_session.name].spec.env["CCACHE_DIR"] == "/native/fixer-cache"
    assert next_session.work_dir == str(path)
    assert (path / "build" / "output.o").read_text() == "cached"


async def test_detached_runner_records_cache_delta_and_build_elapsed(tmp_path):
    from src.jobs.artifacts import atomic_json, read_json
    from tests.test_jobs_runner import launch, request

    tool = tmp_path / "ccache"
    tool.write_text("#!/bin/sh\ncat \"$CCACHE_DIR/counts\"\n")
    tool.chmod(0o700)
    counters = tmp_path / "counts"
    counters.write_text("direct_cache_hit 0\npreprocessed_cache_hit 0\ncache_miss 0\n")
    job = request(tmp_path,
                  "import os; from pathlib import Path; "
                  "Path(os.environ['CCACHE_DIR'], 'counts').write_text("
                  "'direct_cache_hit 3\\npreprocessed_cache_hit 1\\ncache_miss 1\\n')")
    job["preset"] = "build"
    job["contract"]["env"].update(CCACHE_DIR=str(tmp_path))
    job["contract"]["env"]["PATH"] = str(tmp_path) + ":" + job["contract"]["env"]["PATH"]
    job["contract"]["build_cache"] = {"kind": "ccache", "dir": str(tmp_path)}
    atomic_json(tmp_path / "request.json", job)
    proc = await launch(tmp_path, job)
    await proc.wait()
    result = read_json(tmp_path / "result.json")
    assert result["outcome"] == "passed", result
    measured = result["build_measurements"]
    assert measured["cache_hit_rate"] == .8 and measured["cache_hits"] == 4
    assert measured["rebuild_seconds"] > 0 and measured["workspace_path"] == str(tmp_path)


async def test_exact_commit_check_owner_releases_by_its_actual_batch(world):
    from src.integration.train import candidate_head
    from tests.test_jobs import candidate_checks

    await world.db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.LINK,
                                         source_path=str(world.store)))
    (world.store / ".git" / "info" / "exclude").write_text("build/\n")
    await freeze(world, "one")
    batch = Batch(id="one", project_id="p", repository_id="r", target_ref="refs/heads/main")
    checks = candidate_checks(world)
    exact = await checks.for_candidate(batch, world.candidate)
    requested = await exact.request(candidate_head(batch, world.candidate))
    job = await world.db.get_job(requested.job_ids[0])
    assert job["owner_id"].startswith("checks:")
    path = Path(job["contract"]["cwd"])
    (path / "build").mkdir()
    (path / "build" / "output.o").write_text("cached")
    await finish(world.db, job, exit_code=0)
    async with world.db.immediate() as conn:
        await conn.execute(update(integration_batches).where(
            integration_batches.c.id == "one",
        ).values(lifecycle="promoted"))
    await freeze(world, "two")
    batch = Batch(id="two", project_id="p", repository_id="r", target_ref="refs/heads/main")
    exact = await checks.for_candidate(batch, world.candidate)
    requested = await exact.request(candidate_head(batch, world.candidate))
    next_job = await world.db.get_job(requested.job_ids[0])
    assert next_job["contract"]["cwd"] == str(path)
    assert (path / "build" / "output.o").read_text() == "cached"


async def test_candidates_link_primary_clone_when_retained_store_is_separate(world, tmp_path):
    primary = tmp_path / "primary"
    await world.git._arun(["clone", "--no-hardlinks", str(world.store), str(primary)])
    await world.db.create_repo(RepoConfig(id="r", project_id="p", source_type=RepoSourceType.LINK,
                                         source_path=str(primary)))
    async with candidate_workspace(world.db, world.config, request(world), "a") as wid:
        ws = await world.db.get_workspace(wid)
    common = await world.git._arun(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ws.workspace_path,
    )
    assert common.strip() == str(primary / ".git")
    assert await world.git.arev_parse(ws.workspace_path, "HEAD") == world.candidate
    assert (primary / "sample.py").read_text() == "value = 1\n"


async def test_cache_measurement_migration_is_idempotent_on_disposable_database(db):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    migration = importlib.import_module("migrations.versions.a00000000114_build_cache_measurements")

    def exercise(conn):
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
            columns = {c["name"] for c in inspect(conn).get_columns("wait_measurements")}
            assert {"cache_hit_rate", "workspace_path"} <= columns
            migration.downgrade()
            migration.downgrade()
            columns = {c["name"] for c in inspect(conn).get_columns("wait_measurements")}
            assert not {"cache_hit_rate", "workspace_path"} & columns
            migration.upgrade()
            migration.upgrade()

    async with db._engine.begin() as conn:
        await conn.run_sync(exercise)
