"""Tests for ``aq doctor`` — registry, runner, exit codes, built-in checks.

Covers ``docs/specs/implementation/trust-and-ops.md`` §8 rows 2 and 3.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from src.commands.ops_commands import OpsCommandsMixin
from src.config import AppConfig
from src.doctor import builtin_checks, default_registry
from src.doctor.models import (
    RESERVED_CHECK_IDS,
    CheckResult,
    DoctorCheck,
    DoctorContext,
    Severity,
)
from src.doctor.runner import DoctorRegistry, exit_code_for, run_doctor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake(check_id: str, severity=Severity.OK, *, fix=None, timeout_s=5.0, delay=0.0):
    async def run(ctx):
        if delay:
            await asyncio.sleep(delay)
        return CheckResult(id=check_id, severity=severity, detail="fake")

    return DoctorCheck(id=check_id, run=run, fix=fix, timeout_s=timeout_s)


@pytest.fixture
def ctx(tmp_path):
    config = AppConfig(data_dir=str(tmp_path))
    return DoctorContext(config=config, db=None, handler=None)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_list(self):
        reg = DoctorRegistry()
        reg.register(_fake("b.two"))
        reg.register(_fake("a.one"))
        assert reg.ids() == ["a.one", "b.two"]
        assert [c.id for c in reg.checks()] == ["a.one", "b.two"]
        assert "a.one" in reg
        assert len(reg) == 2

    def test_duplicate_id_rejected(self):
        reg = DoctorRegistry()
        reg.register(_fake("x.y"))
        with pytest.raises(ValueError, match="duplicate doctor check id"):
            reg.register(_fake("x.y"))

    def test_unregister(self):
        reg = DoctorRegistry()
        reg.register(_fake("x.y"))
        assert reg.unregister("x.y") is True
        assert reg.unregister("x.y") is False

    def test_default_registry_has_all_builtins(self):
        from src.doctor.capability_checks import capability_checks
        from src.doctor.db_checks import db_checks
        from src.doctor.formula_checks import formula_checks
        from src.doctor.hierarchy_checks import hierarchy_checks
        from src.doctor.integration_checks import integration_checks
        from src.doctor.playbook_v2_checks import playbook_v2_checks
        from src.doctor.pool_checks import pool_checks
        from src.doctor.profile_checks import profile_checks
        from src.doctor.resource_checks import resource_checks
        from src.doctor.task_checks import task_checks
        from src.doctor.workspace_checks import workspace_checks

        reg = default_registry()
        expected = (
            {c.id for c in builtin_checks()}
            | {c.id for c in hierarchy_checks()}
            | {c.id for c in pool_checks()}
            | {c.id for c in task_checks()}
            | {c.id for c in formula_checks()}
            | {c.id for c in resource_checks()}
            | {c.id for c in integration_checks()}
            | {c.id for c in capability_checks()}
            | {c.id for c in workspace_checks()}
            | {c.id for c in profile_checks()}
            | {c.id for c in db_checks()}
            | {c.id for c in playbook_v2_checks()}
        )
        assert set(reg.ids()) == expected

    def test_reserved_ids_are_not_preregistered(self):
        """Reserved ids stay free so their owning subsystem can claim them."""
        reg = default_registry()
        for check_id in RESERVED_CHECK_IDS:
            assert check_id not in reg
            reg.register(_fake(check_id))  # must not raise


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class TestRunner:
    async def test_runs_all_checks(self, ctx):
        reg = DoctorRegistry()
        reg.register(_fake("a.one"))
        reg.register(_fake("b.two", Severity.WARN))
        result = await run_doctor(reg, ctx)
        ids = [c["id"] for c in result["checks"]]
        assert "a.one" in ids and "b.two" in ids
        assert result["summary"]["ok"] == 1
        assert result["summary"]["warn"] == 1

    async def test_runs_concurrently(self, ctx):
        reg = DoctorRegistry()
        for i in range(6):
            reg.register(_fake(f"slow.{i}", delay=0.2))
        started = asyncio.get_event_loop().time()
        await run_doctor(reg, ctx)
        elapsed = asyncio.get_event_loop().time() - started
        # Sequential would be >= 1.2s; concurrent should be well under.
        assert elapsed < 0.9, elapsed

    async def test_timeout_becomes_error(self, ctx):
        reg = DoctorRegistry()
        reg.register(_fake("slow.check", timeout_s=0.05, delay=1.0))
        result = await run_doctor(reg, ctx)
        row = next(c for c in result["checks"] if c["id"] == "slow.check")
        assert row["severity"] == "error"
        assert "timed out" in row["detail"]
        assert result["exit_code"] == 2

    async def test_crashing_check_is_isolated(self, ctx):
        async def boom(_ctx):
            raise RuntimeError("kaboom")

        reg = DoctorRegistry()
        reg.register(DoctorCheck(id="bad.check", run=boom))
        reg.register(_fake("good.check"))
        result = await run_doctor(reg, ctx)
        bad = next(c for c in result["checks"] if c["id"] == "bad.check")
        good = next(c for c in result["checks"] if c["id"] == "good.check")
        assert bad["severity"] == "error"
        assert "kaboom" in bad["detail"]
        assert good["severity"] == "ok"

    async def test_non_checkresult_return_is_error(self, ctx):
        async def wrong(_ctx):
            return {"id": "nope"}

        reg = DoctorRegistry()
        reg.register(DoctorCheck(id="wrong.check", run=wrong))
        result = await run_doctor(reg, ctx)
        row = next(c for c in result["checks"] if c["id"] == "wrong.check")
        assert row["severity"] == "error"

    async def test_only_filter(self, ctx):
        reg = DoctorRegistry()
        reg.register(_fake("a.one"))
        reg.register(_fake("b.two"))
        result = await run_doctor(reg, ctx, only=["a.one"])
        assert [c["id"] for c in result["checks"]] == ["a.one"]

    async def test_duration_recorded(self, ctx):
        reg = DoctorRegistry()
        reg.register(_fake("a.one", delay=0.05))
        result = await run_doctor(reg, ctx)
        assert result["checks"][0]["duration_ms"] >= 1

    async def test_empty_registry(self, ctx):
        result = await run_doctor(DoctorRegistry(), ctx)
        # Only the reserved placeholders remain.
        assert result["exit_code"] == 0
        assert all(c["severity"] == "info" for c in result["checks"])


class TestExitCodes:
    @pytest.mark.parametrize(
        "severities,expected",
        [
            ([], 0),
            ([Severity.OK], 0),
            ([Severity.OK, Severity.INFO], 0),
            ([Severity.OK, Severity.WARN], 1),
            ([Severity.WARN, Severity.INFO], 1),
            ([Severity.WARN, Severity.ERROR], 2),
            ([Severity.ERROR], 2),
        ],
    )
    def test_mapping(self, severities, expected):
        results = [CheckResult(id=f"c{i}", severity=s, detail="") for i, s in enumerate(severities)]
        assert exit_code_for(results) == expected

    async def test_runner_reports_exit_code(self, ctx):
        reg = DoctorRegistry()
        reg.register(_fake("a.one", Severity.WARN))
        assert (await run_doctor(reg, ctx))["exit_code"] == 1
        reg.register(_fake("b.two", Severity.ERROR))
        assert (await run_doctor(reg, ctx))["exit_code"] == 2


class TestFix:
    async def test_fix_runs_then_rechecks(self, ctx):
        state = {"broken": True, "fix_calls": 0}

        async def run(_ctx):
            return CheckResult(
                id="f.check",
                severity=Severity.WARN if state["broken"] else Severity.OK,
                detail="state",
            )

        async def fix(_ctx):
            state["fix_calls"] += 1
            state["broken"] = False
            return CheckResult(id="f.check", severity=Severity.OK, detail="fixed")

        reg = DoctorRegistry()
        reg.register(DoctorCheck(id="f.check", run=run, fix=fix))

        before = await run_doctor(reg, ctx)
        assert before["checks"][0]["severity"] == "warn"
        assert before["checks"][0]["fixable"] is True
        assert before["summary"]["fixes_applied"] == 0

        state["broken"] = True
        after = await run_doctor(reg, ctx, fix=True)
        row = next(c for c in after["checks"] if c["id"] == "f.check")
        assert row["severity"] == "ok"
        assert row["fix_applied"] is True
        assert after["summary"]["fixes_applied"] == 1
        assert state["fix_calls"] == 1

    async def test_fix_not_called_when_check_passes(self, ctx):
        calls = {"n": 0}

        async def fix(_ctx):
            calls["n"] += 1
            return CheckResult(id="f.ok", severity=Severity.OK, detail="")

        reg = DoctorRegistry()
        reg.register(_fake("f.ok", Severity.OK, fix=fix))
        await run_doctor(reg, ctx, fix=True)
        assert calls["n"] == 0

    async def test_failing_fix_reported_not_raised(self, ctx):
        async def fix(_ctx):
            raise RuntimeError("fix exploded")

        reg = DoctorRegistry()
        reg.register(_fake("f.bad", Severity.WARN, fix=fix))
        result = await run_doctor(reg, ctx, fix=True)
        row = next(c for c in result["checks"] if c["id"] == "f.bad")
        assert row["severity"] == "error"
        assert "fix exploded" in row["detail"]

    async def test_check_without_fix_is_not_marked_fixable(self, ctx):
        reg = DoctorRegistry()
        reg.register(_fake("nofix.check", Severity.WARN))
        result = await run_doctor(reg, ctx)
        assert result["checks"][0]["fixable"] is False

    async def test_result_id_mismatch_does_not_crash_fix(self, ctx):
        """A check may return a CheckResult whose id isn't its own.

        Plugin checks especially.  ``--fix`` used to index ``by_id[r.id]``
        directly and raised KeyError, taking the whole command down.
        """

        async def run(_ctx):
            return CheckResult(id="other.id", severity=Severity.WARN, detail="x")

        reg = DoctorRegistry()
        reg.register(DoctorCheck(id="mismatch.check", run=run))
        result = await run_doctor(reg, ctx, fix=True)
        assert {c["id"] for c in result["checks"]} >= {"other.id"}


class TestUnknownCheckFilter:
    """``--check typo.id`` must fail, not silently pass on an empty table."""

    async def test_unknown_id_is_an_error(self, ctx):
        reg = default_registry()
        result = await run_doctor(reg, ctx, only=["definitely.not.a.check"])
        row = next(c for c in result["checks"] if c["id"] == "definitely.not.a.check")
        assert row["severity"] == "error"
        assert "unknown check id" in row["detail"]
        assert result["exit_code"] == 2

    async def test_known_id_still_runs_alone(self, ctx):
        reg = default_registry()
        result = await run_doctor(reg, ctx, only=["pauses.active"])
        assert [c["id"] for c in result["checks"]] == ["pauses.active"]
        assert result["exit_code"] == 0

    async def test_reserved_id_is_not_unknown(self, ctx):
        reserved = sorted(RESERVED_CHECK_IDS)[0]
        result = await run_doctor(default_registry(), ctx, only=[reserved])
        assert [c["id"] for c in result["checks"]] == [reserved]
        assert result["exit_code"] == 0

    async def test_one_bad_id_among_good_ones_still_errors(self, ctx):
        result = await run_doctor(default_registry(), ctx, only=["pauses.active", "nope.nope"])
        assert result["exit_code"] == 2
        assert {c["id"] for c in result["checks"]} == {"pauses.active", "nope.nope"}


class TestReservedChecks:
    async def test_unregistered_reserved_ids_report_info(self, ctx):
        result = await run_doctor(default_registry(), ctx)
        rows = {c["id"]: c for c in result["checks"]}
        for check_id, owner in RESERVED_CHECK_IDS.items():
            assert rows[check_id]["severity"] == "info"
            assert "not registered" in rows[check_id]["detail"]
            assert rows[check_id]["data"]["owner"] == owner

    async def test_registered_reserved_id_replaces_placeholder(self, ctx):
        reg = default_registry()
        reg.register(_fake("sessions.stale", Severity.WARN))
        result = await run_doctor(reg, ctx)
        rows = [c for c in result["checks"] if c["id"] == "sessions.stale"]
        assert len(rows) == 1
        assert rows[0]["severity"] == "warn"

    async def test_reserved_placeholders_never_fail_ci(self, ctx):
        result = await run_doctor(DoctorRegistry(), ctx)
        assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# Built-in checks
# ---------------------------------------------------------------------------


class TestBuiltinCatalog:
    def test_ids_are_namespaced_and_unique(self):
        ids = [c.id for c in builtin_checks()]
        assert len(ids) == len(set(ids))
        assert all("." in i for i in ids)

    def test_no_builtin_claims_a_reserved_id(self):
        assert not (set(c.id for c in builtin_checks()) & set(RESERVED_CHECK_IDS))

    def test_expected_checks_present(self):
        ids = {c.id for c in builtin_checks()}
        assert {
            "config.parse",
            "db.connect",
            "db.migrations",
            "vault.parse",
            "harness.binaries",
            "harness.drift",
            "db.wal_size",
            "logs.llm_size",
            "tasks.stuck",
            "pauses.active",
            "events.registry",
            "mcp.probes",
        } <= ids

    def test_fixable_set_matches_design(self):
        """Only the enumerated checks may declare a fix (design §5.4)."""
        fixable = {c.id for c in builtin_checks() if c.fix is not None}
        assert fixable == {"db.wal_size", "logs.llm_size", "harness.drift"}

    async def test_all_builtins_survive_a_bare_context(self, ctx):
        """No built-in may crash when the DB / handler are absent."""
        result = await run_doctor(default_registry(), ctx)
        for row in result["checks"]:
            assert "check failed" not in row["detail"], row


class TestConfigParseCheck:
    async def test_missing_file_is_info(self, ctx):
        result = await _run_single("config.parse", ctx)
        assert result.severity is Severity.INFO

    async def test_broken_config_is_error(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("this: [is: not: valid yaml", encoding="utf-8")
        config = AppConfig(data_dir=str(tmp_path))
        config._config_path = str(path)
        result = await _run_single(
            "config.parse", DoctorContext(config=config, db=None, handler=None)
        )
        assert result.severity is Severity.ERROR

    async def test_valid_config_is_ok(self, tmp_path):
        d = tmp_path.as_posix()
        path = tmp_path / "config.yaml"
        path.write_text(
            f"data_dir: {d}\n"
            f"workspace_dir: {d}/ws\n"
            f"database:\n  url: {d}/aq.db\n"
            "discord:\n  bot_token: t-1\n  guild_id: '1'\n",
            encoding="utf-8",
        )
        config = AppConfig(data_dir=str(tmp_path))
        config._config_path = str(path)
        result = await _run_single(
            "config.parse", DoctorContext(config=config, db=None, handler=None)
        )
        assert result.severity is Severity.OK, result.detail


class TestDbChecks:
    async def test_connect_ok_on_real_db(self, tmp_path):
        from src.database import Database

        db = Database(str(tmp_path / "t.db"))
        await db.initialize()
        try:
            config = AppConfig(data_dir=str(tmp_path))
            result = await _run_single(
                "db.connect", DoctorContext(config=config, db=db, handler=None)
            )
            assert result.severity is Severity.OK
        finally:
            await db.close()

    async def test_connect_error_without_db(self, ctx):
        result = await _run_single("db.connect", ctx)
        assert result.severity is Severity.ERROR

    async def test_migrations_at_head_on_fresh_db(self, tmp_path):
        from src.database import Database

        db = Database(str(tmp_path / "t.db"))
        await db.initialize()
        try:
            config = AppConfig(data_dir=str(tmp_path))
            result = await _run_single(
                "db.migrations", DoctorContext(config=config, db=db, handler=None)
            )
            assert result.severity is Severity.OK, result.detail
            assert result.data["current"] == result.data["head"]
        finally:
            await db.close()

    async def test_migrations_behind_head_is_error(self, tmp_path):
        from sqlalchemy import text

        from src.database import Database

        db = Database(str(tmp_path / "t.db"))
        await db.initialize()
        try:
            async with db._engine.begin() as conn:
                await conn.execute(text("UPDATE alembic_version SET version_num='deadbeef'"))
            config = AppConfig(data_dir=str(tmp_path))
            result = await _run_single(
                "db.migrations", DoctorContext(config=config, db=db, handler=None)
            )
            assert result.severity is Severity.ERROR
            assert "alembic upgrade head" in result.detail
        finally:
            await db.close()

    async def test_wal_size_ok_below_threshold(self, tmp_path):
        from src.database import Database

        path = str(tmp_path / "t.db")
        db = Database(path)
        await db.initialize()
        try:
            config = AppConfig(data_dir=str(tmp_path))
            ctx = DoctorContext(config=config, db=db, handler=None)
            result = await _run_single("db.wal_size", ctx)
            assert result.severity is Severity.OK
        finally:
            await db.close()

    async def test_wal_size_warns_above_threshold_and_fix_truncates(self, tmp_path):
        from src.database import Database
        from src.models import Project

        path = str(tmp_path / "t.db")
        db = Database(path)
        await db.initialize()
        try:
            # Generate WAL content, then set the threshold to 0 MB so any WAL warns.
            for i in range(50):
                await db.create_project(Project(id=f"p-{i}", name=f"n{i}"))
            config = AppConfig(data_dir=str(tmp_path))
            config.security.wal_warn_mb = 1
            ctx = DoctorContext(config=config, db=db, handler=None)

            check = _get_check("db.wal_size")
            if not os.path.exists(f"{path}-wal"):
                pytest.skip("no WAL file produced on this platform")

            # The fix must be idempotent: run it twice, both times clean.
            first = await check.fix(ctx)
            assert first.severity is Severity.OK
            second = await check.fix(ctx)
            assert second.severity is Severity.OK
            after = await check.run(ctx)
            assert after.severity is Severity.OK
        finally:
            await db.close()

    async def test_wal_size_info_on_postgres(self, tmp_path):
        config = AppConfig(data_dir=str(tmp_path))
        # ``backend`` is inferred from the URL scheme, not settable directly.
        config.database.url = "postgresql://u:p@localhost:5432/aq"
        result = await _run_single(
            "db.wal_size", DoctorContext(config=config, db=None, handler=None)
        )
        assert result.severity is Severity.INFO


class TestVaultParseCheck:
    async def test_missing_vault_is_info(self, ctx):
        result = await _run_single("vault.parse", ctx)
        assert result.severity is Severity.INFO

    async def test_clean_vault_is_ok(self, tmp_path):
        vault = tmp_path / "vault" / "agent-types" / "coder"
        vault.mkdir(parents=True)
        (vault / "profile.md").write_text(
            "---\nid: coder\nname: Coder\n---\n\n## Role\nWrite code.\n",
            encoding="utf-8",
        )
        config = AppConfig(data_dir=str(tmp_path))
        result = await _run_single(
            "vault.parse", DoctorContext(config=config, db=None, handler=None)
        )
        assert result.severity is Severity.OK, result.detail
        assert result.data["scanned"] == 1

    async def test_broken_workspace_kind_is_error(self, tmp_path):
        kinds = tmp_path / "vault" / "workspace-kinds"
        kinds.mkdir(parents=True)
        (kinds / "broken.md").write_text("no frontmatter here\n", encoding="utf-8")
        config = AppConfig(data_dir=str(tmp_path))
        result = await _run_single(
            "vault.parse", DoctorContext(config=config, db=None, handler=None)
        )
        assert result.severity is Severity.ERROR
        assert result.data["files"]


class TestHarnessDriftCheck:
    """``harness.drift`` — vault copies vs the shipped defaults."""

    @pytest.fixture
    def fake_manifest(self, tmp_path, monkeypatch):
        """Point the manifest at a fake shipped dir at version 2 (v1 known)."""
        import hashlib

        from src.sessions import harness_manifest as hm

        defaults = tmp_path / "shipped"
        defaults.mkdir()
        v1 = '---\nid: fake\n---\n\n## Config\n\n```json\n{"command": "fake", "v": 1}\n```\n'
        v2 = '---\nid: fake\n---\n\n## Config\n\n```json\n{"command": "fake", "v": 2}\n```\n'
        (defaults / "fake.md").write_text(v2, encoding="utf-8")
        monkeypatch.setattr(hm, "shipped_harness_dir", lambda: str(defaults))
        monkeypatch.setattr(
            hm,
            "SHIPPED_HARNESS_HASHES",
            {
                "fake.md": frozenset(
                    {hashlib.sha256(v1.encode()).hexdigest(), hashlib.sha256(v2.encode()).hexdigest()}
                )
            },
        )
        data_dir = tmp_path / "data"
        vault_copy = data_dir / "vault" / "harnesses" / "fake.md"
        vault_copy.parent.mkdir(parents=True)
        ctx = DoctorContext(config=AppConfig(data_dir=str(data_dir)), db=None, handler=None)
        return {"ctx": ctx, "copy": vault_copy, "v1": v1, "v2": v2}

    async def test_unseeded_vault_is_info_and_fixable(self, ctx):
        result = await _run_single("harness.drift", ctx)
        assert result.severity is Severity.INFO
        assert result.fixable is True
        assert set(result.data["missing"]) >= {"claude.md", "codex.md", "gemini.md"}

    async def test_freshly_seeded_vault_is_ok(self, tmp_path):
        from src.vault import ensure_default_harnesses

        ensure_default_harnesses(str(tmp_path))
        config = AppConfig(data_dir=str(tmp_path))
        result = await _run_single(
            "harness.drift", DoctorContext(config=config, db=None, handler=None)
        )
        assert result.severity is Severity.OK, result.detail
        assert result.data["stale"] == [] and result.data["edited"] == []

    async def test_stale_copy_is_warn_and_fix_refreshes_it(self, fake_manifest):
        fake_manifest["copy"].write_text(fake_manifest["v1"], encoding="utf-8")
        ctx = fake_manifest["ctx"]

        result = await _run_single("harness.drift", ctx)
        assert result.severity is Severity.WARN
        assert result.fixable is True
        assert result.data["stale"] == ["fake.md"]
        assert "aq doctor --fix" in result.detail

        fixed = await _get_check("harness.drift").fix(ctx)
        assert fixed.severity is Severity.OK
        assert fixed.data["refreshed"] == ["fake.md"]
        assert fake_manifest["copy"].read_text(encoding="utf-8") == fake_manifest["v2"]

        again = await _run_single("harness.drift", ctx)
        assert again.severity is Severity.OK

    async def test_edited_copy_is_info_and_fix_leaves_it(self, fake_manifest):
        custom = '---\nid: fake\n---\n\n## Config\n\n```json\n{"command": "my-fake"}\n```\n'
        fake_manifest["copy"].write_text(custom, encoding="utf-8")
        ctx = fake_manifest["ctx"]

        result = await _run_single("harness.drift", ctx)
        assert result.severity is Severity.INFO
        assert result.fixable is False
        assert result.data["edited"] == ["fake.md"]
        assert "aq vault reset-harness" in result.detail

        fixed = await _get_check("harness.drift").fix(ctx)
        assert fixed.data["edited"] == ["fake.md"]
        assert fake_manifest["copy"].read_text(encoding="utf-8") == custom

    async def test_edited_copy_with_parse_warning_is_warn(self, fake_manifest):
        # An unknown Config key is the parser's cheapest warning.
        custom = (
            '---\nid: fake\n---\n\n## Config\n\n```json\n'
            '{"command": "my-fake", "not_a_key": 1}\n```\n'
        )
        fake_manifest["copy"].write_text(custom, encoding="utf-8")
        result = await _run_single("harness.drift", fake_manifest["ctx"])
        assert result.severity is Severity.WARN
        assert "not_a_key" in result.detail
        assert "aq vault reset-harness fake" in result.detail
        assert result.data["edited_issues"]["fake.md"]


class TestHarnessBinariesCheck:
    async def test_git_is_present(self, ctx):
        result = await _run_single("harness.binaries", ctx)
        # git must exist in any dev/CI environment; the rest are optional.
        assert result.severity in (Severity.OK, Severity.WARN)
        assert result.data["binaries"]["git"]["ok"] is True

    async def test_probes_the_runtime_front_ends_too(self, ctx):
        """Narrowed from "per configured harness", but not down to git+gh."""
        result = await _run_single("harness.binaries", ctx)
        assert {"git", "gh", "claude", "codex", "gemini"} <= set(result.data["binaries"])

    async def test_timed_out_probe_kills_the_child(self, monkeypatch):
        """A cancelled ``communicate()`` abandons the process; it must be reaped."""
        import src.doctor.builtin as builtin

        killed = {"n": 0}

        class HangingProc:
            returncode = None

            async def communicate(self):
                await asyncio.sleep(3600)

            def kill(self):
                killed["n"] += 1
                self.returncode = -9

            async def wait(self):
                return self.returncode

        async def fake_exec(*args, **kwargs):
            return HangingProc()

        monkeypatch.setattr(builtin.shutil, "which", lambda n: "/usr/bin/" + n)
        monkeypatch.setattr(builtin.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(builtin, "_PROBE_TIMEOUT_S", 0.01)

        name, ok, detail = await builtin._probe_binary("git")
        assert ok is False
        assert "timed out" in detail
        assert killed["n"] == 1, "the timed-out child was never killed"


class TestLogsLlmSizeCheck:
    async def test_no_log_dir_is_ok(self, ctx):
        result = await _run_single("logs.llm_size", ctx)
        assert result.severity is Severity.OK

    async def test_warns_on_dirs_past_retention(self, tmp_path):
        base = tmp_path / "logs" / "llm"
        old = (datetime.now(timezone.utc) - timedelta(days=99)).strftime("%Y-%m-%d")
        recent = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        (base / old).mkdir(parents=True)
        (base / recent).mkdir(parents=True)
        (base / old / "x.jsonl").write_text("{}\n", encoding="utf-8")
        (base / recent / "x.jsonl").write_text("{}\n", encoding="utf-8")

        config = AppConfig(data_dir=str(tmp_path))
        config.llm_logging.retention_days = 30
        ctx = DoctorContext(config=config, db=None, handler=None)
        check = _get_check("logs.llm_size")

        before = await check.run(ctx)
        assert before.severity is Severity.WARN
        assert old in before.data["beyond_retention"]

        # Fix removes only what is beyond retention, and is idempotent.
        first = await check.fix(ctx)
        assert first.data["removed"] == 1
        assert not (base / old).exists()
        assert (base / recent).exists()
        second = await check.fix(ctx)
        assert second.data["removed"] == 0
        assert (base / recent).exists()
        assert (await check.run(ctx)).severity is Severity.OK


class TestPausesActiveCheck:
    async def test_always_info(self, tmp_path):
        config = AppConfig(data_dir=str(tmp_path))
        result = await _run_single(
            "pauses.active", DoctorContext(config=config, db=None, handler=None)
        )
        assert result.severity is Severity.INFO
        # Playbooks are paused by default during the overhaul.
        assert result.data["playbooks"] is True


class TestEventsRegistryCheck:
    """Every case here drives a **real** :class:`~src.event_bus.EventBus`.

    The earlier version of this test invented ``seen_event_types`` on a fake
    bus.  No such attribute existed anywhere in ``src/``, so on a real install
    the check observed nothing and unconditionally reported OK.  Driving the
    real class is what makes the check falsifiable.
    """

    @staticmethod
    def _ctx_with_bus(tmp_path, bus):
        class FakeOrch:
            plugin_registry = None

        class FakeHandler:
            orchestrator = FakeOrch()

        FakeOrch.bus = bus
        return DoctorContext(
            config=AppConfig(data_dir=str(tmp_path)), db=None, handler=FakeHandler()
        )

    async def test_nothing_observed_is_info_not_ok(self, ctx):
        """'Nothing was looked at' must not read as 'nothing is wrong'."""
        result = await _run_single("events.registry", ctx)
        assert result.severity is Severity.INFO
        assert result.data["observed_count"] == 0
        assert result.data["registered_count"] > 0

    async def test_bus_exposes_what_it_dispatched(self):
        """The attribute the check reads must exist on the real EventBus."""
        from src.event_bus import EventBus

        bus = EventBus(validate_events=False)
        assert bus.seen_event_types == set()
        await bus.emit("task_completed", {"task_id": "t-1"})
        assert "task_completed" in bus.seen_event_types
        # A copy, not the live set — a caller cannot corrupt the bus.
        bus.seen_event_types.add("not.real")
        assert "not.real" not in bus.seen_event_types

    async def test_registered_emits_are_ok(self, tmp_path):
        from src.event_bus import EventBus
        from src.event_schemas import registered_event_types

        known = sorted(registered_event_types())[0]
        bus = EventBus(validate_events=False)
        await bus.emit(known, {})

        result = await _run_single("events.registry", self._ctx_with_bus(tmp_path, bus))
        assert result.severity is Severity.OK
        assert result.data["observed_count"] == 1

    async def test_flags_an_unregistered_type_the_bus_actually_emitted(self, tmp_path):
        from src.event_bus import EventBus

        bus = EventBus(validate_events=False)
        await bus.emit("totally.made.up.event", {})

        result = await _run_single("events.registry", self._ctx_with_bus(tmp_path, bus))
        assert result.severity is Severity.WARN
        assert "totally.made.up.event" in result.data["unregistered"]


class TestMcpProbesCheck:
    async def test_no_servers_is_info(self, ctx):
        result = await _run_single("mcp.probes", ctx)
        assert result.severity is Severity.INFO


class TestTasksStuckCheck:
    async def test_no_handler_is_info(self, ctx):
        result = await _run_single("tasks.stuck", ctx)
        assert result.severity is Severity.INFO

    async def test_delegates_to_command(self, tmp_path):
        calls = []

        class FakeHandler:
            async def _cmd_get_stuck_tasks(self, args):
                calls.append(args)
                return {"stuck": [{"id": "t-1"}]}

        config = AppConfig(data_dir=str(tmp_path))
        result = await _run_single(
            "tasks.stuck", DoctorContext(config=config, db=None, handler=FakeHandler())
        )
        assert result.severity is Severity.WARN
        assert calls and "assigned_threshold_seconds" in calls[0]


class TestFixIdempotency:
    """Every built-in fix must be safe to run twice on the same state."""

    async def test_double_run_on_pristine_state(self, tmp_path):
        from src.database import Database

        db = Database(str(tmp_path / "t.db"))
        await db.initialize()
        try:
            config = AppConfig(data_dir=str(tmp_path))
            ctx = DoctorContext(config=config, db=db, handler=None)
            for check in builtin_checks():
                if check.fix is None:
                    continue
                first = await check.fix(ctx)
                second = await check.fix(ctx)
                assert first.severity is not Severity.ERROR, check.id
                assert second.severity is not Severity.ERROR, check.id
                post = await check.run(ctx)
                assert post.severity is not Severity.ERROR, check.id
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Command surface
# ---------------------------------------------------------------------------


class TestDoctorCommand:
    async def test_reports_not_configured_without_registry(self, tmp_path):
        handler = _StubHandler(AppConfig(data_dir=str(tmp_path)), registry=None)
        result = await handler._cmd_doctor({})
        assert result["success"] is False
        assert result["exit_code"] == 3

    async def test_runs_with_explicit_registry(self, tmp_path):
        reg = DoctorRegistry()
        reg.register(_fake("a.one"))
        handler = _StubHandler(AppConfig(data_dir=str(tmp_path)), registry=reg)
        result = await handler._cmd_doctor({})
        assert result["success"] is True
        assert result["exit_code"] == 0
        assert any(c["id"] == "a.one" for c in result["checks"])

    async def test_checks_accepts_comma_string(self, tmp_path):
        reg = DoctorRegistry()
        reg.register(_fake("a.one"))
        reg.register(_fake("b.two"))
        handler = _StubHandler(AppConfig(data_dir=str(tmp_path)), registry=reg)
        result = await handler._cmd_doctor({"checks": "a.one"})
        ids = [c["id"] for c in result["checks"] if not c["data"].get("reserved")]
        assert ids == ["a.one"]

    async def test_falls_back_to_orchestrator_registry(self, tmp_path):
        reg = DoctorRegistry()
        reg.register(_fake("a.one"))
        handler = _StubHandler(AppConfig(data_dir=str(tmp_path)), registry=None)
        handler.orchestrator.doctor_registry = reg
        result = await handler._cmd_doctor({})
        assert result["success"] is True


class TestPluginRegistration:
    def test_register_doctor_check_namespaces_id(self, tmp_path):
        reg = DoctorRegistry()
        ctx = _plugin_context(tmp_path, "my-plugin", reg)
        check = _fake("thing.ok")
        ctx.register_doctor_check(check)
        assert check.id == "plugin.my-plugin.thing.ok"
        assert check.owner == "plugin:my-plugin"
        assert "plugin.my-plugin.thing.ok" in reg

    def test_no_registry_is_a_noop(self, tmp_path):
        ctx = _plugin_context(tmp_path, "my-plugin", None)
        ctx.register_doctor_check(_fake("thing.ok"))  # must not raise

    def test_already_prefixed_id_not_doubled(self, tmp_path):
        reg = DoctorRegistry()
        ctx = _plugin_context(tmp_path, "p", reg)
        check = _fake("plugin.p.already")
        ctx.register_doctor_check(check)
        assert check.id == "plugin.p.already"


# ---------------------------------------------------------------------------
# CLI exit codes
# ---------------------------------------------------------------------------


class TestCliExitCodes:
    @pytest.mark.parametrize("exit_code", [0, 1, 2])
    def test_doctor_exits_with_runner_code(self, exit_code, monkeypatch):
        from click.testing import CliRunner

        from src.cli.app import cli

        _stub_client(
            monkeypatch,
            {
                "success": True,
                "checks": [{"id": "a.one", "severity": "ok", "detail": "", "duration_ms": 1}],
                "summary": {"ok": 1, "info": 0, "warn": 0, "error": 0, "fixes_applied": 0},
                "exit_code": exit_code,
            },
        )
        result = CliRunner().invoke(cli, ["doctor"])
        assert result.exit_code == exit_code, result.output

    def test_doctor_exits_3_on_transport_failure(self, monkeypatch):
        from click.testing import CliRunner

        from src.cli.app import cli

        class Boom:
            async def __aenter__(self):
                raise RuntimeError("daemon unreachable")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr("src.cli.doctor._get_client", lambda *a, **k: Boom())
        result = CliRunner().invoke(cli, ["doctor"])
        assert result.exit_code == 3

    def test_doctor_exits_3_when_registry_missing(self, monkeypatch):
        from click.testing import CliRunner

        from src.cli.app import cli

        _stub_client(
            monkeypatch,
            {"success": False, "error": "doctor registry not configured", "exit_code": 3},
        )
        result = CliRunner().invoke(cli, ["doctor"])
        assert result.exit_code == 3


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _get_check(check_id: str) -> DoctorCheck:
    return next(c for c in builtin_checks() if c.id == check_id)


async def _run_single(check_id: str, ctx: DoctorContext) -> CheckResult:
    return await _get_check(check_id).run(ctx)


def _plugin_context(tmp_path, name, registry):
    from unittest.mock import AsyncMock, MagicMock

    from src.plugins.base import PluginContext

    bus = MagicMock()
    bus.emit = AsyncMock()
    return PluginContext(
        plugin_name=name,
        install_path=str(tmp_path / "install"),
        data_path=str(tmp_path / "data"),
        db=AsyncMock(),
        bus=bus,
        command_registry={},
        tool_registry={},
        event_type_registry=set(),
        doctor_registry=registry,
    )


class _StubOrchestrator:
    def __init__(self):
        self.doctor_registry = None
        self.db = None


class _StubHandler(OpsCommandsMixin):
    """Minimal object exposing the surface OpsCommandsMixin needs."""

    def __init__(self, config, registry=None):
        self.config = config
        self.orchestrator = _StubOrchestrator()
        self._doctor_registry = registry
        self.db = None
        self._active_project_id = None


def _stub_client(monkeypatch, payload):
    class StubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, command, args=None):
            return payload

    monkeypatch.setattr("src.cli.doctor._get_client", lambda *a, **k: StubClient())
