"""Part II models, profile config and SwarmConfig — spec §9."""

from __future__ import annotations

import time

import pytest

from src.config import AppConfig, SwarmConfig
from src.database import Database
from src.models import AgentProfile, AgentState, Project, SessionRecord
from src.profiles.parser import VALID_LIFECYCLES, parse_profile

PROJECT_ID = "proj"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    yield database
    await database.close()


def _session(**over):
    now = time.time()
    base = dict(
        id="s1",
        project_id=PROJECT_ID,
        profile_id="worker-standard",
        harness="claude",
        provider="fake",
        name="p-worker-standard--proj--1",
        lifecycle="pool",
        work_dir="/wd",
        epoch="e",
        instance_token="t",
        started_at=now,
        state="running",
    )
    base.update(over)
    return SessionRecord(**base)


class TestSessionRecord:
    async def test_pool_fields_round_trip(self, db):
        await db.create_session(
            _session(
                agent_id="agent-1",
                claims=2,
                claim_phase="active",
                claim_phase_at=1.0,
                last_claim_epoch=3,
                last_claim_result="claimed",
            )
        )
        row = await db.get_session("s1")
        assert (
            row.agent_id,
            row.claims,
            row.claim_phase,
            row.claim_phase_at,
            row.last_claim_epoch,
            row.last_claim_result,
        ) == ("agent-1", 2, "active", 1.0, 3, "claimed")

    async def test_list_sessions_filters_by_agent_and_phase(self, db):
        await db.create_session(
            _session(id="s1", name="p-a", agent_id="agent-1", claim_phase="active")
        )
        await db.create_session(_session(id="s2", name="p-b", agent_id="agent-2"))
        assert [s.id for s in await db.list_sessions(agent_id="agent-1")] == ["s1"]
        assert [s.id for s in await db.list_sessions(claim_phase="active")] == ["s1"]


class TestAgentProfile:
    async def test_pool_columns_round_trip(self, db):
        await db.create_profile(
            AgentProfile(
                id="w",
                name="w",
                lifecycle="pool",
                min_active=1,
                max_active=3,
                max_claims_per_session=None,
            )
        )
        p = await db.get_profile("w")
        assert (p.lifecycle, p.min_active, p.max_active, p.max_claims_per_session) == (
            "pool",
            1,
            3,
            None,
        )

    def test_agent_state_retired(self):
        assert AgentState.RETIRED.value == "RETIRED"


POOL_PROFILE = """---
id: worker-fast
name: Worker fast
---
## Config
```json
{"harness": "claude", "lifecycle": "pool", "min_active": 0, "max_active": 3,
 "max_claims_per_session": 2, "needs_workspace": true}
```
## Role
Fast worker.
"""


class TestProfileParser:
    def test_pool_lifecycle_accepted(self):
        assert "pool" in VALID_LIFECYCLES
        parsed = parse_profile(POOL_PROFILE)
        assert parsed.is_valid, f"Errors: {parsed.errors}"
        assert parsed.config["lifecycle"] == "pool"
        assert parsed.config["max_claims_per_session"] == 2

    @pytest.mark.parametrize(
        "bad", ['"max_claims_per_session": 0', '"max_active": -1', '"min_active": true']
    )
    def test_rejects_zero_negative_and_bool(self, bad):
        text = POOL_PROFILE.replace('"max_claims_per_session": 2', bad)
        parsed = parse_profile(text)
        assert not parsed.is_valid

    def test_pool_keys_rejected_on_task_lifecycle(self):
        text = POOL_PROFILE.replace('"lifecycle": "pool"', '"lifecycle": "task"')
        parsed = parse_profile(text)
        assert not parsed.is_valid


class TestSwarmConfig:
    def test_defaults(self):
        cfg = SwarmConfig()
        assert (
            cfg.enabled,
            cfg.claim_wait_max,
            cfg.max_starts_per_tick,
            cfg.max_drains_per_tick,
            cfg.scale_down_grace,
            cfg.prepare_timeout,
            cfg.max_filings_per_task,
        ) == (False, 60, 2, 5, 120, 120, 20)

    def test_validate_rejects_negative(self):
        cfg = SwarmConfig(prepare_timeout=-1)
        assert cfg.validate()

    def test_app_config_has_swarm(self):
        assert isinstance(AppConfig().swarm, SwarmConfig)
