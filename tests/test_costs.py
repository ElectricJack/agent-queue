"""Tests for cost accounting — ledger columns, rollup, pricing, ``aq costs``.

Covers ``docs/specs/implementation/trust-and-ops.md`` §8, ``tests/test_costs.py``.
The governing rule is honesty: a row is priced only when it carries both a
model that matches a pricing entry and an input/output split.  Everything else
is reported as ``unpriced_tokens`` — never estimated.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from src.commands.ops_commands import OpsCommandsMixin, _parse_since
from src.config import AppConfig, ModelPricing, PricingConfig
from src.database import Database
from src.models import Agent, AgentState, Project


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "costs.db"))
    await database.initialize()
    yield database
    await database.close()


async def _seed(db, *, projects=("p-1",), agents=(("a-1", "claude-sonnet"),)):
    for pid in projects:
        await db.create_project(Project(id=pid, name=pid))
    for aid, profile in agents:
        await db.create_agent(
            Agent(id=aid, name=aid, profile_id=profile, state=AgentState.IDLE)
        )


class TestSchema:
    async def test_pricing_columns_present(self, db):
        from sqlalchemy import inspect

        def _cols(sync_conn):
            return {c["name"] for c in inspect(sync_conn).get_columns("token_ledger")}

        async with db._engine.begin() as conn:
            cols = await conn.run_sync(_cols)
        assert {"model", "input_tokens", "output_tokens"} <= cols

    async def test_columns_are_nullable(self, db):
        await _seed(db)
        # No task row needed for the FK on SQLite with deferred enforcement?
        # Use an explicit task so the insert is valid on every backend.
        from src.models import Task, TaskStatus

        await db.create_task(Task(id="t-1", project_id="p-1", title="x", description="y",
                                  status=TaskStatus.READY))
        await db.record_token_usage("p-1", "a-1", "t-1", 100)
        rows = await db.get_cost_rollup()
        assert rows[0]["model"] is None
        assert rows[0]["input_tokens"] == 0
        assert rows[0]["tokens_used"] == 100


@pytest.fixture
async def seeded(db):
    """A ledger with a priced row, an unpriced row and two projects/profiles."""
    from src.models import Task, TaskStatus

    await db.create_project(Project(id="p-1", name="one"))
    await db.create_project(Project(id="p-2", name="two"))
    for aid, profile, pid in (
        ("a-1", "claude-sonnet-4-5", "p-1"),
        ("a-2", "claude-haiku-4", "p-2"),
    ):
        await db.create_agent(
            Agent(id=aid, name=aid, profile_id=profile, state=AgentState.IDLE)
        )
    for tid, pid in (("t-1", "p-1"), ("t-2", "p-2")):
        await db.create_task(
            Task(id=tid, project_id=pid, title=tid, description="d", status=TaskStatus.READY)
        )

    # Fully attributed row.
    await db.record_token_usage(
        "p-1", "a-1", "t-1", 3000, model="claude-sonnet-4-5", input_tokens=2000,
        output_tokens=1000,
    )
    # Historical row: no model, no split.
    await db.record_token_usage("p-1", "a-1", "t-1", 500)
    # Attributed model with no pricing entry configured.
    await db.record_token_usage(
        "p-2", "a-2", "t-2", 900, model="some-other-model", input_tokens=600, output_tokens=300
    )
    return db


class TestRollup:
    async def test_group_by_project(self, seeded):
        rows = await seeded.get_cost_rollup(group_by="project")
        by_group = {}
        for r in rows:
            by_group.setdefault(r["group"], []).append(r)
        assert set(by_group) == {"p-1", "p-2"}
        assert sum(r["tokens_used"] for r in by_group["p-1"]) == 3500
        assert sum(r["tokens_used"] for r in by_group["p-2"]) == 900

    async def test_group_by_profile(self, seeded):
        rows = await seeded.get_cost_rollup(group_by="profile")
        groups = {r["group"] for r in rows}
        assert groups == {"claude-sonnet-4-5", "claude-haiku-4"}

    async def test_group_by_day(self, seeded):
        rows = await seeded.get_cost_rollup(group_by="day")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert {r["group"] for r in rows} == {today}

    async def test_split_by_model_within_group(self, seeded):
        rows = [r for r in await seeded.get_cost_rollup() if r["group"] == "p-1"]
        models = {r["model"] for r in rows}
        assert models == {"claude-sonnet-4-5", None}

    async def test_project_filter(self, seeded):
        rows = await seeded.get_cost_rollup(project_id="p-2")
        assert {r["group"] for r in rows} == {"p-2"}

    async def test_since_filter_excludes_old_rows(self, seeded):
        future = time.time() + 3600
        assert await seeded.get_cost_rollup(since_ts=future) == []

    async def test_invalid_group_by_raises(self, seeded):
        with pytest.raises(ValueError):
            await seeded.get_cost_rollup(group_by="nonsense")

    async def test_empty_ledger(self, db):
        assert await db.get_cost_rollup() == []

    async def test_entries_counted(self, seeded):
        rows = [r for r in await seeded.get_cost_rollup() if r["model"] is None]
        assert rows[0]["entries"] == 1


class TestPricingMatch:
    def test_first_glob_wins(self):
        pricing = PricingConfig(
            models=[
                ModelPricing(model="claude-sonnet-4-5*", input_per_mtok=3.0, output_per_mtok=15.0),
                ModelPricing(model="claude-*", input_per_mtok=1.0, output_per_mtok=5.0),
            ]
        )
        assert pricing.match("claude-sonnet-4-5-20260101").input_per_mtok == 3.0
        assert pricing.match("claude-haiku-4").input_per_mtok == 1.0

    def test_no_match_returns_none(self):
        pricing = PricingConfig(models=[ModelPricing(model="gpt-*")])
        assert pricing.match("claude-sonnet") is None

    def test_empty_table_matches_nothing(self):
        assert PricingConfig().match("anything") is None


class TestParseSince:
    @pytest.mark.parametrize("raw,seconds", [("7d", 7 * 86400), ("12h", 12 * 3600),
                                             ("30m", 1800), ("2w", 2 * 604800)])
    def test_relative(self, raw, seconds):
        parsed = _parse_since(raw)
        assert abs((time.time() - seconds) - parsed) < 5

    def test_iso_date(self):
        expected = datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
        assert _parse_since("2026-01-15") == expected

    def test_empty_is_none(self):
        assert _parse_since(None) is None
        assert _parse_since("") is None
        assert _parse_since("   ") is None

    def test_garbage_raises(self):
        with pytest.raises(ValueError, match="unrecognised 'since'"):
            _parse_since("last tuesday")


class _Handler(OpsCommandsMixin):
    def __init__(self, db, config):
        self.db = db
        self.config = config
        self.orchestrator = None
        self._doctor_registry = None
        self._active_project_id = None


def _config(entries=()):
    config = AppConfig()
    config.pricing = PricingConfig(models=list(entries))
    return config


class TestGetCostsCommand:
    async def test_prices_only_fully_attributed_rows(self, seeded):
        handler = _Handler(
            seeded,
            _config(
                [ModelPricing(model="claude-sonnet-4-5*", input_per_mtok=3.0, output_per_mtok=15.0)]
            ),
        )
        result = await handler._cmd_get_costs({})
        assert result["success"] is True

        priced = [r for r in result["rows"] if r["cost_usd"] is not None]
        assert len(priced) == 1
        # 2000 input @ $3/Mtok + 1000 output @ $15/Mtok = 0.006 + 0.015
        assert priced[0]["cost_usd"] == pytest.approx(0.021)
        assert result["total_cost_usd"] == pytest.approx(0.021)

        # The historical row (500) and the unmatched-model row (900) stay unpriced.
        assert result["unpriced_tokens"] == 1400
        assert all(r["cost_usd"] is None for r in result["rows"] if r["model"] != "claude-sonnet-4-5")

    async def test_never_estimates_without_pricing_table(self, seeded):
        handler = _Handler(seeded, _config())
        result = await handler._cmd_get_costs({})
        assert result["total_cost_usd"] == 0.0
        assert result["unpriced_tokens"] == 3000 + 500 + 900
        assert all(r["cost_usd"] is None for r in result["rows"])
        assert result["pricing_models"] == []

    async def test_row_with_model_but_no_split_is_unpriced(self, db):
        from src.models import Task, TaskStatus

        await db.create_project(Project(id="p-1", name="one"))
        await db.create_agent(
            Agent(id="a-1", name="a", profile_id="prof", state=AgentState.IDLE)
        )
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="t", description="d", status=TaskStatus.READY)
        )
        await db.record_token_usage("p-1", "a-1", "t-1", 1000, model="claude-sonnet-4-5")

        handler = _Handler(
            db,
            _config(
                [ModelPricing(model="claude-sonnet-4-5*", input_per_mtok=3.0, output_per_mtok=15.0)]
            ),
        )
        result = await handler._cmd_get_costs({})
        assert result["unpriced_tokens"] == 1000
        assert result["rows"][0]["cost_usd"] is None

    async def test_mixed_bucket_does_not_lose_the_unsplit_tokens(self, db):
        """A bucket holding split *and* unsplit rows for the same model.

        ``get_cost_rollup`` groups by ``(group, model)``, so both land in one
        row.  Pricing that row off the split sum alone silently dropped the
        unsplit tokens from both ``cost_usd`` and ``unpriced_tokens`` —
        exactly what design §7's honesty rule forbids.
        """
        from src.models import Task, TaskStatus

        await db.create_project(Project(id="p-1", name="one"))
        await db.create_agent(
            Agent(id="a-1", name="a", profile_id="prof", state=AgentState.IDLE)
        )
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="t", description="d", status=TaskStatus.READY)
        )
        # Same model, same project, same day: one entry with a split, one without.
        await db.record_token_usage(
            "p-1",
            "a-1",
            "t-1",
            3000,
            model="claude-sonnet-4-5",
            input_tokens=2000,
            output_tokens=1000,
        )
        await db.record_token_usage("p-1", "a-1", "t-1", 700, model="claude-sonnet-4-5")

        handler = _Handler(
            db,
            _config(
                [ModelPricing(model="claude-sonnet-4-5*", input_per_mtok=3.0, output_per_mtok=15.0)]
            ),
        )
        result = await handler._cmd_get_costs({})

        assert len(result["rows"]) == 1, "the two entries must share one bucket"
        row = result["rows"][0]
        assert row["tokens_used"] == 3700
        assert row["cost_usd"] == pytest.approx(0.021)
        assert row["unpriced_tokens"] == 700
        assert result["unpriced_tokens"] == 700
        assert result["total_cost_usd"] == pytest.approx(0.021)

    async def test_every_token_is_either_priced_or_reported_unpriced(self, seeded):
        """The accounting identity: nothing falls between the two buckets."""
        handler = _Handler(
            seeded,
            _config(
                [ModelPricing(model="claude-sonnet-4-5*", input_per_mtok=3.0, output_per_mtok=15.0)]
            ),
        )
        result = await handler._cmd_get_costs({})
        for row in result["rows"]:
            split = (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0)
            priced_tokens = split if row["cost_usd"] is not None else 0
            assert priced_tokens + row["unpriced_tokens"] == row["tokens_used"], row
        assert result["unpriced_tokens"] == sum(r["unpriced_tokens"] for r in result["rows"])

    async def test_group_by_validated(self, seeded):
        handler = _Handler(seeded, _config())
        assert "error" in await handler._cmd_get_costs({"group_by": "quarter"})

    async def test_bad_since_reports_error(self, seeded):
        handler = _Handler(seeded, _config())
        result = await handler._cmd_get_costs({"since": "yesterday-ish"})
        assert "unrecognised 'since'" in result["error"]

    async def test_since_relative_window(self, seeded):
        handler = _Handler(seeded, _config())
        recent = await handler._cmd_get_costs({"since": "1d"})
        assert recent["rows"]
        old = datetime.now(timezone.utc) + timedelta(days=2)
        future = await handler._cmd_get_costs({"since": old.strftime("%Y-%m-%d")})
        assert future["rows"] == []

    async def test_project_filter_and_group_by_profile(self, seeded):
        handler = _Handler(seeded, _config())
        result = await handler._cmd_get_costs({"project_id": "p-2", "group_by": "profile"})
        assert {r["group"] for r in result["rows"]} == {"claude-haiku-4"}

    async def test_pricing_model_reported_per_row(self, seeded):
        handler = _Handler(
            seeded,
            _config([ModelPricing(model="claude-*", input_per_mtok=1.0, output_per_mtok=5.0)]),
        )
        result = await handler._cmd_get_costs({})
        matched = [r for r in result["rows"] if r["pricing_model"]]
        assert matched and matched[0]["pricing_model"] == "claude-*"


class TestRecordTokenUsage:
    async def test_split_persisted(self, db):
        from src.models import Task, TaskStatus

        await db.create_project(Project(id="p-1", name="one"))
        await db.create_agent(
            Agent(id="a-1", name="a", profile_id="prof", state=AgentState.IDLE)
        )
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="t", description="d", status=TaskStatus.READY)
        )
        await db.record_token_usage(
            "p-1", "a-1", "t-1", 300, model="m-1", input_tokens=200, output_tokens=100
        )
        rows = await db.get_cost_rollup()
        assert rows[0]["model"] == "m-1"
        assert rows[0]["input_tokens"] == 200
        assert rows[0]["output_tokens"] == 100

    async def test_legacy_positional_call_still_works(self, db):
        """Existing call sites pass only the total — they must not break."""
        from src.models import Task, TaskStatus

        await db.create_project(Project(id="p-1", name="one"))
        await db.create_agent(
            Agent(id="a-1", name="a", profile_id="prof", state=AgentState.IDLE)
        )
        await db.create_task(
            Task(id="t-1", project_id="p-1", title="t", description="d", status=TaskStatus.READY)
        )
        await db.record_token_usage("p-1", "a-1", "t-1", 42)
        assert await db.get_project_token_usage("p-1") == 42


class TestLedgerSurvivesLifecycle:
    """The ledger is an audit record — routine GC must not erase spend.

    Regression for ``token_audit`` reporting zero tokens.  ``token_ledger``
    used to carry real FKs to ``agents.id`` and ``tasks.id``, so
    ``archive_task`` and ``delete_agent`` both had to cascade into it.  Since
    archiving is the *normal* end of a completed task and agents are reaped
    whenever their profile stops resolving, that meant essentially all spend
    was deleted moments after it was recorded.
    """

    async def _seed_spend(self, db):
        from src.models import Task, TaskStatus

        await db.create_project(Project(id="p-1", name="one"))
        await db.create_agent(
            Agent(id="a-1", name="a", profile_id="prof", state=AgentState.IDLE)
        )
        await db.create_task(
            Task(
                id="t-1",
                project_id="p-1",
                title="t",
                description="d",
                status=TaskStatus.COMPLETED,
            )
        )
        await db.record_token_usage(
            "p-1", "a-1", "t-1", 300, model="m-1", input_tokens=200, output_tokens=100
        )

    async def test_archiving_a_task_keeps_its_spend(self, db):
        await self._seed_spend(db)
        assert await db.archive_task("t-1") is True

        assert await db.get_project_token_usage("p-1") == 300
        audit = await db.get_token_audit(days=7)
        assert audit["total"] == 300

    async def test_archived_task_still_named_in_top_tasks(self, db):
        """The audit outer-joins ``tasks`` then backfills from the archive."""
        await self._seed_spend(db)
        await db.archive_task("t-1")

        audit = await db.get_token_audit(days=7)
        top = {t["task_id"]: t for t in audit["top_tasks"]}
        assert top["t-1"]["tokens"] == 300
        # Title comes from archived_tasks, not a bare id with a null title.
        assert top["t-1"]["title"] == "t"
        assert top["t-1"]["archived"] is True

    async def test_reaping_an_agent_keeps_its_spend(self, db):
        await self._seed_spend(db)
        await db.delete_agent("a-1")

        assert await db.get_project_token_usage("p-1") == 300
        audit = await db.get_token_audit(days=7)
        assert audit["total"] == 300
        # Attribution degrades gracefully rather than dropping the row.
        rollup = await db.get_cost_rollup(group_by="profile")
        assert sum(r["tokens_used"] for r in rollup) == 300

    async def test_audit_survives_archive_and_reap_together(self, db):
        """The real-world sequence: task completes, is archived, agent reaped."""
        await self._seed_spend(db)
        await db.archive_task("t-1")
        await db.delete_agent("a-1")

        audit = await db.get_token_audit(days=7)
        assert audit["total"] == 300
        assert audit["by_project"] == [
            {"project_id": "p-1", "project_name": "one", "tokens": 300, "task_count": 1}
        ]
        assert audit["daily"] and sum(d["tokens"] for d in audit["daily"]) == 300

    async def test_deleting_the_project_still_purges_the_ledger(self, db):
        """``project_id`` keeps its FK — an explicit purge must still work."""
        await self._seed_spend(db)
        await db.delete_project("p-1")

        audit = await db.get_token_audit(days=7)
        assert audit["total"] == 0


class TestConfigWiring:
    """``pricing:`` is a YAML *list*; ``security:`` is a map (spec §2)."""

    def _base_yaml(self, d: str) -> str:
        return (
            f"data_dir: {d}\n"
            f"workspace_dir: {d}/ws\n"
            f"database:\n  url: {d}/aq.db\n"
            "discord:\n  bot_token: t\n  guild_id: '1'\n"
        )

    def test_pricing_parsed_from_list_form(self, tmp_path):
        from src.config import load_config

        path = tmp_path / "config.yaml"
        path.write_text(
            self._base_yaml(tmp_path.as_posix())
            + "pricing:\n"
            "  - {model: 'claude-sonnet-4-5*', input_per_mtok: 3.0, output_per_mtok: 15.0}\n"
            "  - {model: 'claude-haiku-*', input_per_mtok: 1.0, output_per_mtok: 5.0}\n",
            encoding="utf-8",
        )
        config = load_config(str(path))
        assert [m.model for m in config.pricing.models] == [
            "claude-sonnet-4-5*",
            "claude-haiku-*",
        ]
        assert config.pricing.match("claude-sonnet-4-5-x").output_per_mtok == 15.0

    def test_pricing_parsed_from_mapping_form(self, tmp_path):
        from src.config import load_config

        path = tmp_path / "config.yaml"
        path.write_text(
            self._base_yaml(tmp_path.as_posix())
            + "pricing:\n  models:\n    - {model: 'gpt-*', input_per_mtok: 2.0}\n",
            encoding="utf-8",
        )
        config = load_config(str(path))
        assert [m.model for m in config.pricing.models] == ["gpt-*"]

    def test_security_section_parsed(self, tmp_path):
        from src.config import load_config

        path = tmp_path / "config.yaml"
        path.write_text(
            self._base_yaml(tmp_path.as_posix())
            + "security:\n"
            "  env_scrub_enabled: false\n"
            "  env_allowlist: ['OPENAI_API_KEY', '*_TOKEN']\n"
            "  wal_warn_mb: 128\n"
            "  llm_log_warn_mb: 1024\n",
            encoding="utf-8",
        )
        config = load_config(str(path))
        assert config.security.env_scrub_enabled is False
        assert config.security.env_allowlist == ["OPENAI_API_KEY", "*_TOKEN"]
        assert config.security.wal_warn_mb == 128
        assert config.security.llm_log_warn_mb == 1024

    def test_negative_prices_rejected(self):
        from src.config import ModelPricing, PricingConfig

        errors = PricingConfig(models=[ModelPricing(model="m", input_per_mtok=-1)]).validate()
        assert errors

    def test_empty_glob_rejected(self):
        from src.config import ModelPricing, PricingConfig

        assert PricingConfig(models=[ModelPricing(model="")]).validate()

    def test_security_thresholds_validated(self):
        from src.config import SecurityConfig

        assert SecurityConfig(wal_warn_mb=0).validate()
        assert SecurityConfig(llm_log_warn_mb=-1).validate()
        assert SecurityConfig(env_allowlist=["  "]).validate()
        assert SecurityConfig().validate() == []

    def test_both_sections_appear_in_the_config_editor_schema(self):
        from src.config_editor import build_config_schema

        props = build_config_schema()["properties"]
        assert "security" in props and "pricing" in props
        assert props["pricing"]["properties"]["models"]["type"] == "array"

    def test_pricing_list_survives_a_round_trip_write(self, tmp_path):
        """The ruamel writer must not mangle the pricing list."""
        from src.config import load_config
        from src.config_editor import write_section

        path = tmp_path / "config.yaml"
        path.write_text(
            self._base_yaml(tmp_path.as_posix())
            + "pricing:\n"
            "  # keep me\n"
            "  - {model: 'claude-sonnet-4-5*', input_per_mtok: 3.0, output_per_mtok: 15.0}\n",
            encoding="utf-8",
        )
        write_section(str(path), "security", {"wal_warn_mb": 99})
        text = path.read_text(encoding="utf-8")
        assert "keep me" in text, "round-trip writer dropped a comment"
        config = load_config(str(path))
        assert [m.model for m in config.pricing.models] == ["claude-sonnet-4-5*"]
        assert config.security.wal_warn_mb == 99


async def test_projectless_usage_is_recorded_without_a_project_and_included_in_totals(db):
    await db.record_token_usage(
        None, "global-session", "", 120, model="test-model",
        input_tokens=100, output_tokens=20,
    )
    assert await db.list_projects() == []
    rows = await db.get_cost_rollup()
    assert sum(row["tokens_used"] for row in rows) == 120
    assert sum(row["input_tokens"] for row in rows) == 100
    assert sum(row["output_tokens"] for row in rows) == 20
    assert await db.get_cost_rollup(project_id="real-project") == []
