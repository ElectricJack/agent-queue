"""``db.alembic_orphan`` — the repair path for a poisoned ``alembic_version``.

The shape being tested is the 2026-09-02 incident's aftermath: production is
stamped with a revision that exists only on an unmerged branch, so Alembic
cannot walk the chain and the daemon will not start.  The check has to name
*where* the revision comes from, and ``--fix`` has to undo it rather than lie
about it.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest

from src.config import AppConfig, DatabaseConfig
from src.database import Database
from src.doctor.db_checks import (
    CHECK_ID,
    STAMP_ENV,
    _parent_revision,
    db_checks,
    find_revision_source,
)
from src.doctor.models import DoctorContext, Severity
from src.doctor.runner import apply_fix
from tests.pg_dsn import create_scratch_database

#: ``src.doctor`` re-exports a *function* named ``db_checks``, which shadows
#: the submodule of the same name for a plain ``import`` — same convention as
#: ``pool_checks``.  Reach the module explicitly to monkeypatch inside it.
db_checks_module = importlib.import_module("src.doctor.db_checks")


class TestSharedMemory:
    @staticmethod
    def context(url="postgresql+asyncpg://test:secret@localhost:5533/agent_queue"):
        return DoctorContext(config=AppConfig(database=DatabaseConfig(url=url)))

    @staticmethod
    def probe(monkeypatch, *, size=1024**3, total=1000, used=200, ports=None, failure=None):
        calls = []

        async def output(*args):
            calls.append(args)
            if args[0] == "context":
                return '"unix:///var/run/docker.sock"'
            if args[0] == "ps":
                return "container123"
            if args[0] == "inspect":
                return json.dumps({"size": size, "ports": ports if ports is not None else {
                    "5432/tcp": [{"HostPort": "5533", "HostIp": "0.0.0.0"}],
                }})
            if failure:
                raise failure
            return ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    f"shm {total} {used} {total-used} 20% /dev/shm")

        monkeypatch.setattr(db_checks_module.shutil, "which", lambda _: "/usr/bin/docker")
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.setattr(db_checks_module, "_docker_output", output)
        return calls

    @pytest.mark.parametrize(("size", "used", "severity"), [
        (64 * 1024**2, 200, Severity.WARN),
        (1024**3, 801, Severity.WARN),
        (256 * 1024**2, 800, Severity.OK),
        (1024**3, 200, Severity.OK),
    ])
    async def test_capacity_and_usage_thresholds(self, monkeypatch, size, used, severity):
        calls = self.probe(monkeypatch, size=size, used=used)
        check = next(c for c in db_checks() if c.id == "db.shared_memory")
        result = await check.run(self.context())
        assert check.fix is None
        assert result.severity == severity
        assert result.complete
        assert result.data["shm_size_bytes"] == size
        assert result.data["used_bytes"] == used * 1024
        assert calls[-1] == ("exec", "container123", "df", "-Pk", "/dev/shm")
        if severity == Severity.WARN:
            assert "shm_size: 1g" in result.detail

    @pytest.mark.parametrize("url", [
        "not-a-url",
        "postgresql://db.internal:5533/aq",
        "postgresql:///aq",
        "postgresql://localhost:5533/aq?host=db.internal",
        "postgresql://localhost:5533/aq?port=5534",
    ])
    async def test_unproven_local_endpoint_never_probes_docker(self, monkeypatch, url):
        calls = self.probe(monkeypatch)
        result = await db_checks_module._check_shared_memory(self.context(url))
        assert calls == []
        assert result.execution_status == "unavailable"
        assert not result.complete
        assert "secret" not in result.detail

    async def test_wrong_container_port_never_measures_unrelated_tmpfs(self, monkeypatch):
        calls = self.probe(monkeypatch, ports={"5432/tcp": [{
            "HostPort": "9999", "HostIp": "0.0.0.0",
        }]})
        result = await db_checks_module._check_shared_memory(self.context())
        assert not result.complete
        assert all(call[0] != "exec" for call in calls)

    @pytest.mark.parametrize("size", [64 * 1024**2, 1024**3])
    async def test_failed_usage_probe_preserves_known_capacity(self, monkeypatch, size):
        self.probe(monkeypatch, size=size, failure=TimeoutError())
        result = await db_checks_module._check_shared_memory(self.context())
        assert result.data["shm_size_bytes"] == size
        assert result.severity == (Severity.WARN if size < 256 * 1024**2 else Severity.INFO)
        assert result.execution_status == "unavailable"
        assert not result.complete

    async def test_missing_docker_is_unavailable(self, monkeypatch):
        calls = self.probe(monkeypatch)
        monkeypatch.setattr(db_checks_module.shutil, "which", lambda _: None)
        result = await db_checks_module._check_shared_memory(self.context())
        assert not calls
        assert result.execution_status == "unavailable"

    @pytest.mark.parametrize("matches", ["", "first\nsecond"])
    async def test_absent_or_ambiguous_container_is_unavailable(self, monkeypatch, matches):
        self.probe(monkeypatch)
        original = db_checks_module._docker_output

        async def output(*args):
            return matches if args[0] == "ps" else await original(*args)

        monkeypatch.setattr(db_checks_module, "_docker_output", output)
        result = await db_checks_module._check_shared_memory(self.context())
        assert result.execution_status == "unavailable"
        assert not result.complete

    async def test_remote_docker_override_never_measures_a_container(self, monkeypatch):
        calls = self.probe(monkeypatch)
        monkeypatch.setenv("DOCKER_HOST", "ssh://other-host")
        result = await db_checks_module._check_shared_memory(self.context())
        assert [call[0] for call in calls] == ["context"]
        assert result.execution_status == "unavailable"

    @pytest.mark.parametrize("failure", ["denied", "malformed", "ports", "zero"])
    async def test_failed_or_invalid_probe_never_claims_health(self, monkeypatch, failure):
        from src.doctor.models import ObservationUnavailable

        self.probe(monkeypatch)
        original = db_checks_module._docker_output

        async def output(*args):
            if args[0] == "inspect":
                if failure == "denied":
                    raise ObservationUnavailable("Docker access denied")
                if failure == "malformed":
                    return "not json"
                if failure == "ports":
                    return '{"size": 1073741824, "ports": null}'
            if args[0] == "exec" and failure == "zero":
                return "shm 0 0 0 0% /dev/shm"
            return await original(*args)

        monkeypatch.setattr(db_checks_module, "_docker_output", output)
        result = await db_checks_module._check_shared_memory(self.context())
        assert result.execution_status == "unavailable"
        assert not result.complete

    @pytest.mark.parametrize("timeout", [False, True])
    async def test_probe_reaps_subprocess_on_cancellation_or_timeout(self, monkeypatch, timeout):
        import asyncio
        from unittest.mock import AsyncMock, Mock

        error = TimeoutError() if timeout else asyncio.CancelledError()
        process = Mock(returncode=None)
        process.communicate = AsyncMock(side_effect=[error, (b"", None)])
        spawn = AsyncMock(return_value=process)
        monkeypatch.setattr(db_checks_module.asyncio, "create_subprocess_exec", spawn)
        with pytest.raises(type(error)):
            await db_checks_module._docker_output("exec", "container", "df", "-Pk", "/dev/shm")
        process.kill.assert_called_once_with()
        assert process.communicate.await_count == 2

    def test_canonical_compose_preserves_cluster_identity_and_capacity(self):
        import yaml

        compose = yaml.safe_load((Path(__file__).resolve().parents[1] /
                                  "docker-compose.yml").read_text())
        assert compose["name"] == "agent-queue2"
        postgres = compose["services"]["postgres"]
        assert postgres["container_name"] == "aq-postgres"
        assert postgres["volumes"] == ["pgdata:/var/lib/postgresql"]
        assert postgres["restart"] == "unless-stopped"
        assert postgres["shm_size"] == "1g"
        assert compose["services"]["postgres-test"]["shm_size"] == "1g"

#: The revision that actually broke the daemon on 2026-09-02.  It has since
#: been merged, so this repo *can* find its file — which makes it the right
#: fixture for the "traced to a branch" case.
REAL_ORPHAN = "f2a4c6e8b0d2"

#: A revision no ref declares, for the "cannot be traced" case.  Invented on
#: purpose: asserting on a real id would make the test depend on which
#: branches happen to be checked out.
ORPHAN = "0badc0ffee00"


@pytest.fixture
async def ctx(tmp_path):
    """A doctor context over a real, fully migrated PostgreSQL database.

    A scratch database of its own, not a pooled lease: these tests stamp
    ``alembic_version`` at a bogus revision and run repairs against it.
    """

    dsn = await create_scratch_database("doctor_db")
    db = Database(dsn)
    await db.initialize()
    config = AppConfig(data_dir=str(tmp_path), database=DatabaseConfig(url=dsn))
    try:
        yield DoctorContext(config=config, db=db, handler=None)
    finally:
        await db.close()


def _pg(dsn: str) -> str:
    return str(dsn).replace("postgresql+asyncpg://", "postgresql://")


async def _stamp(dsn, revision: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(_pg(dsn))
    try:
        await conn.execute("UPDATE alembic_version SET version_num = $1", revision)
    finally:
        await conn.close()


def _stamp_sync(dsn, revision: str) -> None:
    """``_stamp`` callable from a synchronous callback inside a running loop.

    ``command.downgrade`` is monkeypatched with a plain function, so it cannot
    await and cannot ``asyncio.run`` (the test's own loop is already running).
    A short-lived thread gets its own loop.
    """
    import asyncio
    import threading

    error: list[BaseException] = []

    def _run() -> None:
        try:
            asyncio.run(_stamp(dsn, revision))
        except BaseException as exc:  # pragma: no cover - surfaced below
            error.append(exc)

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()
    if error:
        raise error[0]


async def _stamped_at(dsn) -> str:
    import asyncpg

    conn = await asyncpg.connect(_pg(dsn))
    try:
        return await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()


def _check():
    return db_checks()[0]


def _synthetic_revision(revision: str, parent: str) -> str:
    """A revision file whose ``downgrade()`` drops a table we can observe."""
    return (
        f'revision: str = "{revision}"\n'
        f'down_revision: str | None = "{parent}"\n'
        "branch_labels = None\n"
        "depends_on = None\n"
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        "def upgrade() -> None:\n"
        '    op.create_table("orphan_leftover", sa.Column("id", sa.Integer, primary_key=True))\n'
        "def downgrade() -> None:\n"
        '    op.drop_table("orphan_leftover")\n'
    )


def _fixed_text(source: str):
    """A ``_revision_file_text`` that always returns *source*."""

    async def _text(ref: str, path: str) -> str:
        return source

    return _text


def _fixed_source(ref: str, path: str):
    """A ``find_revision_source`` that always reports *ref*/*path*."""

    async def _find(revision: str):
        return ref, path

    return _find


class TestCheck:
    async def test_registered_with_a_fix(self):
        check = _check()
        assert check.id == CHECK_ID
        assert check.fix is not None

    async def test_clean_database_is_ok(self, ctx):
        result = await _check().run(ctx)
        assert result.severity is Severity.OK
        assert result.data["stamped"] == result.data["heads"]

    async def test_uninitialised_database_is_info_not_error(self, tmp_path):
        bare = DoctorContext(config=AppConfig(data_dir=str(tmp_path)), db=None, handler=None)
        result = await _check().run(bare)
        assert result.severity is Severity.INFO

    async def test_orphan_is_an_error_that_names_the_revision(self, ctx, tmp_path):
        await _stamp(ctx.config.database.url, ORPHAN)
        result = await _check().run(ctx)
        assert result.severity is Severity.ERROR
        assert result.fixable
        assert result.data["orphans"] == [ORPHAN]
        assert ORPHAN in result.detail
        assert "refuse to start" in result.detail

    async def test_an_unfindable_orphan_points_at_the_stamp_opt_in(self, ctx, tmp_path):
        await _stamp(ctx.config.database.url, ORPHAN)
        result = await _check().run(ctx)
        # This revision exists on no ref, so the message must not promise a
        # downgrade it cannot perform.
        assert "not found on any local or remote ref" in result.detail
        assert STAMP_ENV in result.detail

    async def test_a_traceable_orphan_names_the_branch_and_file(self, ctx, tmp_path, monkeypatch):
        monkeypatch.setattr(
            db_checks_module,
            "find_revision_source",
            _fixed_source("refs/remotes/origin/aq/bold-dune-47", "migrations/versions/x.py"),
        )
        await _stamp(ctx.config.database.url, ORPHAN)
        result = await _check().run(ctx)
        assert "defined on refs/remotes/origin/aq/bold-dune-47" in result.detail
        assert "migrations/versions/x.py" in result.detail
        assert "--fix" in result.detail


class TestProvenance:
    async def test_a_real_revision_is_traced_to_the_file_that_declares_it(self):
        """``f2a4c6e8b0d2`` is the incident's revision; find where it lives."""
        found = await find_revision_source(REAL_ORPHAN)
        if found is None:
            pytest.skip("no ref in this checkout declares the revision")
        _ref, path = found
        assert path.startswith("migrations/versions/")
        assert REAL_ORPHAN in path, "must return the file that declares it"

    async def test_a_merge_revision_that_only_mentions_it_is_not_the_source(self):
        """A merge revision lists parents in ``down_revision``, not ``revision``.

        A substring search reports that file instead of the real one, and the
        fix then tries to downgrade through a tuple parent and fails.
        """
        found = await find_revision_source(REAL_ORPHAN)
        if found is None:
            pytest.skip("no ref in this checkout declares the revision")
        assert "merge" not in found[1]

    async def test_an_invented_revision_is_not_found(self):
        assert await find_revision_source(ORPHAN) is None

    async def test_a_non_revision_string_is_never_turned_into_a_regex(self):
        assert await find_revision_source("a' or 1=1 --") is None
        assert await find_revision_source("") is None


class TestParentRevision:
    def test_plain_assignment(self):
        assert _parent_revision('down_revision = "abc123"') == "abc123"

    def test_annotated_assignment(self):
        source = 'down_revision: str | None = "abc123"\n'
        assert _parent_revision(source) == "abc123"

    def test_base_revision_has_no_parent(self):
        assert _parent_revision("down_revision = None") is None

    def test_branch_merge_tuple_is_not_a_single_parent(self):
        assert _parent_revision('down_revision = ("a", "b")') is None

    def test_unparseable_source(self):
        assert _parent_revision("def (") is None

    def test_absent_declaration(self):
        assert _parent_revision('revision = "abc"') is None


class TestFix:
    async def test_fix_refuses_to_stamp_past_an_unfindable_orphan(self, ctx, tmp_path):
        """Stamping leaves the orphan's DDL in place — it needs its own opt-in."""
        await _stamp(ctx.config.database.url, ORPHAN)
        result = await apply_fix(_check(), ctx)
        assert result.severity is Severity.ERROR
        assert "fix failed" in result.detail
        assert STAMP_ENV in result.detail
        assert "no revision file on any ref" in result.detail
        # And it changed nothing.
        assert await _stamped_at(ctx.config.database.url) == ORPHAN

    @pytest.mark.migration
    async def test_a_traceable_orphan_is_undone_by_its_own_downgrade(
        self, ctx, tmp_path, monkeypatch
    ):
        """The repair that matters: run the orphan's ``downgrade()``.

        Stands in for the incident exactly — production stamped with a
        revision that lives on an unmerged branch, whose DDL is still
        applied.  ``--fix`` borrows the file from that branch, downgrades
        through it, and leaves the database at the parent this checkout
        knows, so the daemon can boot and upgrade normally.
        """
        dsn = ctx.config.database.url
        head = (await _check().run(ctx)).data["heads"][0]
        import asyncpg

        conn = await asyncpg.connect(_pg(dsn))
        try:
            await conn.execute("CREATE TABLE orphan_leftover (id INTEGER PRIMARY KEY)")
        finally:
            await conn.close()
        await _stamp(ctx.config.database.url, ORPHAN)

        source = _synthetic_revision(ORPHAN, head)
        monkeypatch.setattr(
            db_checks_module, "find_revision_source", _fixed_source("refs/x", "migrations/v.py")
        )
        monkeypatch.setattr(db_checks_module, "_revision_file_text", _fixed_text(source))

        result = await apply_fix(_check(), ctx)
        assert result.severity is Severity.OK, result.detail
        assert await _stamped_at(dsn) == head
        conn = await asyncpg.connect(_pg(dsn))
        try:
            tables = {
                r["tablename"]
                for r in await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
        finally:
            await conn.close()
        assert "orphan_leftover" not in tables, "the orphan's downgrade() must have run"
        assert not list((db_checks_module._VERSIONS_DIR).glob("_orphan_*.py")), (
            "the borrowed revision file must not be left behind"
        )

    async def test_fix_never_writes_into_the_checkouts_versions_directory(
        self, ctx, tmp_path, monkeypatch
    ):
        """The borrowed file must live in a private directory, not the repo.

        Writing it into the real ``migrations/versions/`` and unlinking it
        afterwards mutates state every concurrent Alembic scan reads: a
        pytest-xdist worker (or an operator's second shell) that enumerates
        the directory during that window sees the file appear and vanish
        mid-scan and fails with ``Can't find Python file``.  The fix has to
        point Alembic at a temp dir *in addition to* the checkout's versions
        instead.
        """
        from alembic import command

        head = (await _check().run(ctx)).data["heads"][0]
        await _stamp(ctx.config.database.url, ORPHAN)
        source = _synthetic_revision(ORPHAN, head)
        monkeypatch.setattr(
            db_checks_module, "find_revision_source", _fixed_source("refs/x", "migrations/v.py")
        )
        monkeypatch.setattr(db_checks_module, "_revision_file_text", _fixed_text(source))

        versions_dir = db_checks_module._VERSIONS_DIR
        pycache = versions_dir / "__pycache__"
        pycache.mkdir(exist_ok=True)
        sentinel = pycache / "_doctor_sentinel.pyc"
        sentinel.write_bytes(b"")
        seen: dict[str, object] = {}

        def _observe(cfg, target, **_kwargs):
            seen["versions_written"] = sorted(p.name for p in versions_dir.glob("_orphan_*.py"))
            seen["locations"] = cfg.get_main_option("version_locations")
            seen["target"] = target
            # what the real downgrade() would leave behind
            _stamp_sync(ctx.config.database.url, target)

        monkeypatch.setattr(command, "downgrade", _observe)
        try:
            result = await apply_fix(_check(), ctx)
            assert result.severity is Severity.OK, result.detail
            assert seen["target"] == head
            assert seen["versions_written"] == [], "borrowed file was written into the checkout"
            locations = [Path(part) for part in str(seen["locations"]).split(os.pathsep) if part]
            assert versions_dir.resolve() in [loc.resolve() for loc in locations], (
                "Alembic must still see the checkout's own revisions"
            )
            borrowed = [loc for loc in locations if loc.resolve() != versions_dir.resolve()]
            assert len(borrowed) == 1, seen["locations"]
            assert not borrowed[0].exists(), "the private directory must be cleaned up"
            assert sentinel.exists(), "the checkout's __pycache__ must not be deleted"
        finally:
            sentinel.unlink(missing_ok=True)

    @pytest.mark.migration
    async def test_the_stamp_opt_in_restores_a_bootable_database(self, ctx, tmp_path, monkeypatch):
        monkeypatch.setenv(STAMP_ENV, "1")
        await _stamp(ctx.config.database.url, ORPHAN)
        result = await apply_fix(_check(), ctx)
        assert result.severity is Severity.OK, result.detail
        assert result.fix_applied


@pytest.mark.parametrize("mode", ["accepted", "too_far", "breaking", "missing"])
async def test_retained_migrations_doctor_never_downgrades_or_stamps(
    ctx, tmp_path, monkeypatch, mode
):
    import shutil
    from sqlalchemy import text
    from src.database.schema_key import alembic_head_revisions
    from tests.test_additive_migrations import revision

    retained = tmp_path / "retained"
    shutil.copytree(db_checks_module._VERSIONS_DIR, retained)
    body = "op.drop_table('tasks')" if mode == "breaking" else "pass"
    (retained / "ahead.py").write_text(revision(ORPHAN, alembic_head_revisions()[0], body=body))
    ctx.config.database.schema_ahead_migrations = str(
        retained if mode != "missing" else tmp_path / "absent"
    )
    ctx.config.database.schema_ahead_max_revisions = 0 if mode == "too_far" else 1
    await _stamp(ctx.config.database.url, ORPHAN)
    monkeypatch.setenv(STAMP_ENV, "1")
    for method in ("find_revision_source", "_downgrade_with_borrowed_file", "_stamp"):
        monkeypatch.setattr(
            db_checks_module, method, lambda *a: pytest.fail("must preserve retained schema")
        )
    checked = await _check().run(ctx)
    fixed = await _check().fix(ctx)
    expected = Severity.OK if mode == "accepted" else Severity.ERROR
    assert checked.severity is expected and fixed.severity is expected
    assert not checked.fixable and not fixed.fixable
    assert await _stamped_at(ctx.config.database.url) == ORPHAN
    async with ctx.db._engine.connect() as conn:
        assert await conn.scalar(text("SELECT to_regclass('public.tasks')")) == "tasks"


async def _offline_cli(ctx, monkeypatch, *args):
    """Exercise the real CLI and check against only this test's scratch DB."""
    import asyncio
    import json

    from click.testing import CliRunner

    from src.cli.app import cli

    monkeypatch.setattr("src.database.migration_guard.current_scope", lambda: "cli")
    monkeypatch.delenv("AQ_API_TOKEN", raising=False)
    monkeypatch.setattr("src.cli.db._load_config", lambda: ctx.config)
    monkeypatch.setattr(
        "src.cli.doctor._get_client", lambda *a: pytest.fail("offline must not use the daemon")
    )
    monkeypatch.setattr(
        Database, "initialize", lambda *a: pytest.fail("offline must never initialize the schema")
    )
    result = await asyncio.to_thread(
        CliRunner().invoke, cli,
        ["doctor", "--offline", "--check", CHECK_ID, "--json", *args],
    )
    return result, json.loads(result.stdout)["data"]


class TestOfflineDoctor:
    async def test_check_runs_without_a_daemon_on_an_orphan_database(self, ctx, monkeypatch):
        await _stamp(ctx.config.database.url, ORPHAN)

        async def missing(revision):
            return None

        monkeypatch.setattr(db_checks_module, "find_revision_source", missing)

        result, data = await _offline_cli(ctx, monkeypatch)

        assert result.exit_code == 2, result.output
        assert data["checks"][0]["data"]["orphans"] == [ORPHAN]
        assert data["checks"][0]["severity"] == "error"
        assert await _stamped_at(ctx.config.database.url) == ORPHAN

    async def test_fix_keeps_the_stamp_fallback_opt_in(self, ctx, monkeypatch):
        await _stamp(ctx.config.database.url, ORPHAN)
        monkeypatch.delenv(STAMP_ENV, raising=False)

        async def missing(revision):
            return None

        monkeypatch.setattr(db_checks_module, "find_revision_source", missing)
        result, data = await _offline_cli(ctx, monkeypatch, "--fix")

        assert result.exit_code == 2, result.output
        assert STAMP_ENV in data["checks"][0]["detail"]
        assert not data["checks"][0]["fix_applied"]
        assert await _stamped_at(ctx.config.database.url) == ORPHAN

    @pytest.mark.migration
    async def test_fix_runs_the_borrowed_revisions_downgrade_without_a_daemon(
        self, ctx, monkeypatch,
    ):
        from sqlalchemy import text

        dsn = ctx.config.database.url
        head = (await _check().run(ctx)).data["heads"][0]
        async with ctx.db._engine.begin() as conn:
            await conn.execute(text("CREATE TABLE orphan_leftover (id INTEGER PRIMARY KEY)"))
        await _stamp(dsn, ORPHAN)
        monkeypatch.setattr(
            db_checks_module, "find_revision_source", _fixed_source("refs/x", "migrations/v.py")
        )
        monkeypatch.setattr(
            db_checks_module, "_revision_file_text", _fixed_text(_synthetic_revision(ORPHAN, head))
        )

        result, data = await _offline_cli(ctx, monkeypatch, "--fix")

        assert result.exit_code == 0, result.output
        assert data["checks"][0]["fix_applied"]
        assert await _stamped_at(dsn) == head
        async with ctx.db._engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('public.orphan_leftover')")) is None

    async def test_retained_migration_policy_blocks_offline_repair(self, ctx, monkeypatch):
        await _stamp(ctx.config.database.url, ORPHAN)
        ctx.config.database.schema_ahead_migrations = "/nonexistent/retained/migrations"
        monkeypatch.setenv(STAMP_ENV, "1")
        for method in ("find_revision_source", "_downgrade_with_borrowed_file", "_stamp"):
            monkeypatch.setattr(
                db_checks_module, method, lambda *a: pytest.fail("must preserve retained schema")
            )

        result, data = await _offline_cli(ctx, monkeypatch, "--fix")

        assert result.exit_code == 2, result.output
        assert data["checks"][0]["data"]["retained_migrations"]
        assert await _stamped_at(ctx.config.database.url) == ORPHAN
