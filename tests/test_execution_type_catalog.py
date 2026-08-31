"""Execution types describe configured workers, independently of target tasks."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.agents.execution_types import (
    ExecutionIdentity,
    execution_type_key,
    resolve_execution_catalog,
)
from src.database import Database
from src.intelligence_classes import IntelligenceClass
from src.models import Agent, AgentProfile, AgentState, Project, SessionRecord
from src.sessions.harness_parser import Harness
from src.sessions.harness_registry import HarnessRegistry
from src.sessions.spec import SessionSpecBuilder
from tests.pg_dsn import ensure_worker_postgres_dsn

POSTGRES_DSN = ensure_worker_postgres_dsn()


@pytest.fixture(params=["sqlite", "postgresql"] if POSTGRES_DSN else ["sqlite"])
async def db(request, tmp_path):
    if request.param == "postgresql":
        from src.database.adapters.postgresql import PostgreSQLDatabaseAdapter

        database = PostgreSQLDatabaseAdapter(POSTGRES_DSN)
        await database.initialize()
        await database.reset_for_tests()
    else:
        database = Database(str(tmp_path / "catalog.db"))
        await database.initialize()
    try:
        yield database
    finally:
        await database.close()


def test_distinct_saved_overrides_have_stable_distinct_keys():
    base = ExecutionIdentity("worker", "fixture-cli", "fixture", "model-a", "standard", "")
    assert execution_type_key(base) == execution_type_key(replace(base))
    assert len(execution_type_key(base)) == 64
    for field, value in (
        ("profile_id", "project:p:worker"),
        ("harness", "other-cli"),
        ("provider", "other"),
        ("model", "model-b"),
        ("intelligence_class", "deep"),
        ("reasoning_effort", "high"),
    ):
        assert execution_type_key(base) != execution_type_key(replace(base, **{field: value}))


@pytest.fixture
def launch():
    registry = HarnessRegistry()
    registry.upsert(
        Harness(
            id="claude",
            name="Fixture",
            command="fixture",
            model_flag="--model",
            effort_flag="--effort",
        )
    )
    registry.upsert(Harness(id="codex", name="Codex", command="codex", model_flag="--model"))
    builder = SessionSpecBuilder(
        SimpleNamespace(),
        intelligence_classes={
            "standard": IntelligenceClass(
                "standard",
                "Standard",
                "",
                {
                    "anthropic": {"model": "fixture-standard"},
                    "codex": {"model": "fixture-codex-standard"},
                },
            ),
            "deep": IntelligenceClass(
                "deep",
                "Deep",
                "",
                {
                    "anthropic": {"model": "fixture-deep"},
                    "codex": {"model": "fixture-codex-deep"},
                },
            ),
        },
    )
    return builder, registry


async def seed(db):
    await db.create_project(Project("p", "Project"))
    await db.create_profile(
        AgentProfile(
            id="worker",
            name="Worker",
            harness="claude",
            default_class="standard",
        )
    )


async def catalog(db, launch, project_id="p"):
    builder, registry = launch
    return await resolve_execution_catalog(
        db, project_id, builder=builder, harness_registry=registry
    )


async def test_busy_and_interactive_workers_keep_type_but_are_not_idle(db, launch):
    await seed(db)
    for aid, state in (
        ("z-idle", AgentState.IDLE),
        ("a-busy", AgentState.BUSY),
        ("m-terminal", AgentState.IDLE),
    ):
        await db.create_agent(Agent(aid, aid, "worker", state=state))
    await db.create_session(
        SessionRecord(
            id="interactive",
            project_id=None,
            profile_id="worker",
            harness="claude",
            provider="fake",
            name="interactive",
            lifecycle="named",
            work_dir="/tmp",
            epoch="epoch",
            instance_token="token",
            started_at=1,
            agent_id="m-terminal",
        )
    )
    result = await catalog(db, launch)
    assert len(result.types) == 1
    key = next(iter(result.types))
    assert result.members[key] == ("a-busy", "m-terminal", "z-idle")
    assert result.idle_counts[key] == 1
    assert result.diagnostics == ()


async def test_disabled_deleted_retired_and_control_workers_are_excluded(db, launch):
    await seed(db)
    for aid, attrs in (
        ("disabled", {"enabled": False}),
        ("deleted", {"deleted_at": 1}),
        ("retired", {"state": AgentState.RETIRED}),
        ("supervisor", {"role": "supervisor"}),
        ("triage", {"role": "triage"}),
    ):
        await db.create_agent(Agent(aid, aid, "worker", **attrs))
    result = await catalog(db, launch)
    assert result.types == result.members == result.idle_counts == {}
    assert result.diagnostics == ()


async def test_scope_and_saved_overrides_resolve_without_profile_class_cross_product(db, launch):
    await seed(db)
    await db.create_profile(
        AgentProfile(
            id="project:p:worker",
            name="Scoped",
            harness="codex",
            default_class="deep",
        )
    )
    await db.create_profile(
        AgentProfile(
            id="unused",
            name="Unused",
            harness="claude",
            default_class="standard",
        )
    )
    await db.create_agent(Agent("a", "A", "worker", intelligence_class="deep"))
    await db.create_agent(Agent("b", "B", "worker", model="fixture-fixed"))
    result = await catalog(db, launch)
    assert {(i.profile_id, i.model, i.intelligence_class) for i in result.types.values()} == {
        ("project:p:worker", "fixture-codex-deep", "deep"),
        ("project:p:worker", "fixture-fixed", "deep"),
    }
    assert {
        (i.profile_id, i.model) for i in (await catalog(db, launch, "other")).types.values()
    } == {
        ("worker", "fixture-deep"),
        ("worker", "fixture-fixed"),
    }
    assert (await db.get_profile("project:p:worker")).model == ""


async def test_invalid_codex_reasoning_is_diagnostic_and_launch_omits_it(db, launch):
    await seed(db)
    builder, registry = launch
    builder._intelligence_classes["invalid"] = IntelligenceClass(
        "invalid",
        "Invalid",
        "",
        {"codex": {"model": "fixture-codex", "reasoning_effort": "turbo"}},
    )
    await db.create_agent(
        Agent("bad", "Bad", "worker", harness="codex", intelligence_class="invalid")
    )

    result = await catalog(db, launch)
    assert result.types == {}
    assert result.diagnostics == (
        {
            "agent_id": "bad",
            "code": "invalid_reasoning_effort",
            "message": "Codex reasoning effort 'turbo' is unsupported",
        },
    )

    from src.agents.configuration import apply_agent_overrides

    profile = apply_agent_overrides(await db.get_profile("worker"), await db.get_agent("bad"))
    spec = builder.build_named_spec(
        profile=profile,
        harness=registry.get("codex"),
        project_id="p",
        work_dir="/tmp",
        session_id="s",
        instance_token="token",
    )
    assert all("model_reasoning_effort" not in arg for arg in spec.command)


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"profile_id": "missing"}, "missing_profile"),
        ({"harness": "missing"}, "missing_harness"),
        ({"intelligence_class": "missing"}, "missing_class"),
    ],
)
async def test_invalid_definitions_are_diagnostics_not_candidates(db, launch, changes, code):
    await seed(db)
    await db.create_agent(
        Agent("bad", "Bad", "worker", **{k: v for k, v in changes.items() if k != "profile_id"})
    )
    if "profile_id" in changes:
        await db.update_agent("bad", profile_id=changes["profile_id"])
    result = await catalog(db, launch)
    assert not result.types
    assert result.diagnostics[0]["agent_id"] == "bad"
    assert result.diagnostics[0]["code"] == code


async def test_missing_class_and_model_are_not_inferred_from_task_defaults(db, launch):
    await seed(db)
    await db.update_profile("worker", default_class="")
    await db.create_agent(Agent("a", "A", "worker"))
    result = await catalog(db, launch)
    assert not result.types and result.diagnostics[0]["code"] == "missing_class"
    await db.update_profile("worker", default_class="standard")
    launch[0]._intelligence_classes["standard"] = IntelligenceClass("standard", "", "", {})
    result = await catalog(db, launch)
    assert not result.types and result.diagnostics[0]["code"] == "missing_model"


async def test_catalog_and_real_codex_launch_use_same_model_and_reasoning(db, launch):
    await seed(db)
    builder, registry = launch
    registry.upsert(Harness(id="codex", name="Codex", command="codex", model_flag="--model"))
    builder._intelligence_classes["deep"] = IntelligenceClass(
        "deep",
        "Deep",
        "",
        {
            "openai": {"model": "fixture-api", "reasoning_effort": "low"},
            "codex": {"model": "fixture-cli-deep", "reasoning_effort": "high"},
        },
    )
    await db.create_agent(Agent("a", "A", "worker", harness="codex", intelligence_class="deep"))
    result = await catalog(db, launch)
    identity = next(iter(result.types.values()))
    assert (identity.model, identity.reasoning_effort, identity.provider) == (
        "fixture-cli-deep",
        "high",
        "openai",
    )
    from src.agents.configuration import apply_agent_overrides

    profile = apply_agent_overrides(await db.get_profile("worker"), await db.get_agent("a"))
    spec = builder.build_named_spec(
        profile=profile,
        harness=registry.get("codex"),
        project_id="p",
        work_dir="/tmp",
        session_id="s",
        instance_token="token",
    )
    assert spec.command[spec.command.index("--model") + 1] == identity.model
    assert 'model_reasoning_effort="high"' in spec.command
