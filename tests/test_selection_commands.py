"""Claim and operator boundaries for explicit test selection, without live Jev."""

from unittest.mock import AsyncMock

import pytest

from src.commands.principal import (
    TRUSTED_LOCAL,
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.git.manager import GitManager
from src.models import Project
from src.profiles.capabilities import CapabilityPolicy
from src.test_selection.service import SelectionService
from src.test_selection.static_impact import FixedStaticImpact
from tests.db_fixtures import seed_task_session_attempt
from tests.selection_fixture_repo import build_fixture_repo

COMMANDS = (
    "test_select",
    "test_selection_recheck",
    "test_selection_observe",
    "test_selection_show",
    "test_selection_list",
    "test_selection_policy_show",
    "test_selection_promote",
    "test_selection_revoke",
)


def principal(kind=PrincipalKind.SESSION, **changes):
    return ExecutionPrincipal(
        kind=kind,
        policy=CapabilityPolicy.from_namespaces(aq_commands=COMMANDS),
        session_id=changes.pop("session_id", "s"),
        project_id=changes.pop("project_id", "p"),
        **changes,
    )


@pytest.fixture
async def setup(command_handler_factory, tmp_path):
    h = await command_handler_factory()
    repo = build_fixture_repo(tmp_path / "repo")
    await h.db.create_project(Project(id="p", name="p"))
    await seed_task_session_attempt(h.db, task_id="t", project_id="p", session_id="s")
    await h.db.update_task("t", claim_epoch=7)
    await h.db.update_session("s", work_dir=str(repo), last_claim_epoch=7)
    h.config.test_selection.enabled = True
    h.orchestrator.git = GitManager()
    h.orchestrator._get_default_branch = AsyncMock(return_value="main")
    h.orchestrator.test_selection_service = SelectionService(
        db=h.db,
        git=h.orchestrator.git,
        config_getter=lambda: h.config.test_selection,
        static=FixedStaticImpact({"tests/test_a.py"}),
        transport_factory=lambda cfg: None,
        default_branch_getter=AsyncMock(return_value="main"),
    )
    try:
        yield h, repo
    finally:
        await h.db.close()


async def run(h, name="test_select", args=None, who=None):
    with principal_context(who or principal()):
        return await h.execute(name, args or {"claim_epoch": 7})


@pytest.mark.parametrize("who", [TRUSTED_LOCAL, principal(), ExecutionPrincipal.service("test")])
async def test_disabled(setup, who):
    h, _ = setup
    h.config.test_selection.enabled = False
    assert (await run(h, who=who))["error_code"] == "disabled"


async def test_claimed_workspace_and_event(setup):
    h, repo = setup
    result = await run(h)
    assert result["success"] and result["recorded"]
    assert result["record"]["workspace"] == str(repo)
    assert result["record"]["claim_epoch"] == 7
    assert result["record"]["task_id"] == "t"
    assert result["full_suite_authorized"] is False
    h.orchestrator.bus.emit.assert_any_await(
        "test_selection.recorded.v1",
        {
            "project_id": "p",
            "selection_id": result["selection_id"],
            "mode": "shadow",
            "task_id": "t",
            "full_required": False,
            "jev_status": "disabled",
            "final_count": len(result["final_modules"]),
            "fallback_count": len(result["fallback_modules"]),
        },
    )


@pytest.mark.parametrize(
    "args,code",
    [
        ({"workspace": "/tmp", "claim_epoch": 7}, "spoofed_workspace"),
        ({"workspace": None, "claim_epoch": 7}, "spoofed_workspace"),
        ({"mode": "shadow"}, "stale_claim"),
        ({"claim_epoch": 6}, "stale_claim"),
        ({"task_id": "other", "claim_epoch": 7}, "out_of_scope"),
        ({"project_id": "other", "claim_epoch": 7}, "out_of_scope"),
        ({"mode": "enforce"}, "enforce_not_enabled"),
        ({"mode": "bogus"}, "invalid_mode"),
    ],
)
async def test_refusals(setup, args, code):
    h, _ = setup
    assert (await run(h, args=args))["error_code"] == code


async def test_unheld_and_stale_session(setup):
    h, _ = setup
    await h.db.update_session("s", last_claim_epoch=6)
    assert (await run(h))["error_code"] == "stale_claim"
    await h.db.update_session("s", task_id=None)
    assert (await run(h))["error_code"] == "out_of_scope"


async def test_enforce_flags(setup):
    h, _ = setup
    h.config.test_selection.enforce_enabled = True
    result = await run(h, args={"mode": "enforce", "narrowing_flags": ["-k"]})
    assert result["error_code"] == "narrowing_flags"


async def test_local_workspace_and_service(setup, tmp_path):
    h, repo = setup
    assert (await run(h, args={"project_id": "p"}, who=TRUSTED_LOCAL))[
        "error_code"
    ] == "workspace_required"
    assert (await run(h, args={"project_id": "p", "workspace": str(tmp_path)}, who=TRUSTED_LOCAL))[
        "error_code"
    ] == "workspace_invalid"
    result = await run(h, args={"project_id": "p", "workspace": str(repo)}, who=TRUSTED_LOCAL)
    assert result["success"] and result["full_suite_authorized"]
    assert (await run(h, who=ExecutionPrincipal.service("test")))["error_code"] == "out_of_scope"


async def test_record_scope_recheck_and_observe(setup):
    h, repo = setup
    sid = (await run(h))["selection_id"]
    args = {"selection_id": sid}
    assert (await run(h, "test_selection_recheck", args))["stale"] is False
    (repo / "src/pkg/a.py").write_text("def alpha():\n    return 2\n")
    assert (await run(h, "test_selection_recheck", args))["stale"] is True
    obs = await run(
        h,
        "test_selection_observe",
        {
            **args,
            "exit_code": 0,
            "duration_ms": 10,
            "executed_modules": ["tests/test_a.py"],
        },
    )
    assert obs["success"] and obs["observation_id"]
    shown = await run(h, "test_selection_show", args)
    assert shown["selection"]["id"] == sid
    assert shown["observations"][0]["source"] == "worker"
    await h.db.update_session("s", task_id=None)
    for name in ("test_selection_recheck", "test_selection_observe", "test_selection_show"):
        assert (await run(h, name, args))["error_code"] == "out_of_scope"
        assert (await run(h, name, {"selection_id": "absent"}))["error_code"] == "not_found"


async def test_promotion_revoke_policy_and_list(setup):
    h, _ = setup
    selected = await run(h)
    record = selected["record"]
    args = {
        "project_id": "p",
        "model": record["jev_requested_model"],
        **{
            k: record[k]
            for k in (
                "question_schema_version",
                "catalogue_digest",
                "rules_digest",
                "policy_digest",
            )
        },
        "evidence": {"recall": 1.0},
    }
    promoted = await run(h, "test_selection_promote", args, TRUSTED_LOCAL)
    assert promoted["success"]
    assert (await run(h, "test_selection_promote", args, TRUSTED_LOCAL))[
        "error_code"
    ] == "promotion_active"
    policy = await run(h, "test_selection_policy_show", {"project_id": "p"}, TRUSTED_LOCAL)
    assert policy["promotion"] == promoted["promotion"]
    assert policy["config"]["enabled"] is True
    assert policy["latest_digests"]["catalogue_digest"] == record["catalogue_digest"]
    assert (
        len((await run(h, "test_selection_list", {"project_id": "p"}, TRUSTED_LOCAL))["selections"])
        == 1
    )
    revoke_args = {"promotion_id": promoted["promotion"]["id"], "reason": "critical miss"}
    for expected in (True, False):
        assert (await run(h, "test_selection_revoke", revoke_args, TRUSTED_LOCAL))[
            "revoked"
        ] is expected
    for who in (
        principal(),
        principal(elevated=True, project_id=None),
        ExecutionPrincipal.service("test"),
    ):
        for name, values in (
            ("test_selection_promote", args),
            ("test_selection_revoke", revoke_args),
        ):
            assert (await run(h, name, values, who))["error_code"] == "out_of_scope"


def test_contracts_registered():
    from src.commands.contracts.builtin import register_builtin_contracts
    from src.commands.contracts.registry import ContractRegistry

    registry = ContractRegistry()
    register_builtin_contracts(registry)
    assert all(registry.get(name) is not None for name in COMMANDS)


@pytest.mark.parametrize("kind", [PrincipalKind.SESSION, PrincipalKind.PLAYBOOK])
async def test_full_fallback_does_not_authorize_worker_full_suite(setup, kind):
    h, _ = setup
    result = await run(h, args={"claim_epoch": 7, "base_ref": "missing-base"}, who=principal(kind))
    assert result["success"] and result["full_required"]
    assert result["full_suite_authorized"] is False
    assert any(o["kind"] == "full_suite" for o in result["pending_obligations"])


async def test_contract_preserves_explicit_workspace_null(setup):
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.builtin import set_handler_provider
    from src.commands.contracts.test_selection import TestSelectArgs as SelectArgs

    h, _ = setup
    set_handler_provider(lambda: h)
    try:
        command = CONTRACTS.require("test_select")
        result = await command.invoke(SelectArgs(claim_epoch=7), principal())
        assert result.outcome == "selected"
        assert result.value.record["task_id"] == "t"
        result = await command.invoke(SelectArgs(claim_epoch=7, workspace=None), principal())
        assert result.outcome == "rejected"
        assert "workspace is derived" in result.summary
    finally:
        set_handler_provider(None)


async def test_service_factory_is_cached_without_credentials(setup, monkeypatch):
    h, _ = setup
    del h.orchestrator.test_selection_service
    monkeypatch.delenv(h.config.test_selection.api_key_env, raising=False)
    service = h._test_selection_service()
    assert service is h._test_selection_service()
    assert service._transport_factory(h.config.test_selection) is None
    assert (await run(h))["success"]


async def test_observation_source_and_project_list_scope(setup):
    h, repo = setup
    selected = await run(h, args={"workspace": str(repo), "project_id": "p"}, who=TRUSTED_LOCAL)
    args = {
        "selection_id": selected["selection_id"],
        "exit_code": 0,
        "duration_ms": 10,
        "executed_modules": [],
    }
    assert (await run(h, "test_selection_observe", args, TRUSTED_LOCAL))["success"]
    shown = await run(
        h, "test_selection_show", {"selection_id": selected["selection_id"]}, TRUSTED_LOCAL
    )
    assert shown["observations"][0]["source"] == "local"
    assert (await run(h, "test_selection_list", {"project_id": "p"}))[
        "error_code"
    ] == "out_of_scope"
    elevated = principal(elevated=True)
    assert (await run(h, "test_selection_list", {"project_id": "p"}, elevated))["success"]
    assert (await run(h, "test_selection_policy_show", {"project_id": "q"}, elevated))[
        "error_code"
    ] == "out_of_scope"


async def test_generated_client_and_typed_workspace_boundary(setup):
    import httpx

    from src.api import dependencies as deps
    from src.api.app import create_app
    from tests.test_api_client_contract import _import_repo_client

    _import_repo_client()
    from agent_queue_api_client.api.test_selection import test_select as select_api
    from agent_queue_api_client.client import Client
    from agent_queue_api_client.models.test_select_request import TestSelectRequest as SelectRequest
    from agent_queue_api_client.models.test_select_response import (
        TestSelectResponse as SelectResponse,
    )
    from agent_queue_api_client.models.test_select_response_422 import (
        TestSelectResponse422 as Refusal,
    )

    h, repo = setup
    saved = (
        deps._orchestrator,
        deps._command_handler,
        deps._token_store,
        deps._require_session_token,
    )
    h.orchestrator._command_handler = h
    try:
        app = create_app(h.orchestrator, h.config)
        client = Client(base_url="http://test", raise_on_unexpected_status=False)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            client.set_async_httpx_client(http)
            response = await select_api.asyncio(
                client=client,
                body=SelectRequest.from_dict(
                    {
                        "workspace": str(repo),
                        "project_id": "p",
                        "jev": False,
                    }
                ),
            )
            assert isinstance(response, SelectResponse)
            assert response.full_suite_authorized is True
            assert response.record.to_dict()["workspace"] == str(repo)
            h.config.test_selection.enabled = False
            refusal = await select_api.asyncio(client=client, body=SelectRequest())
            assert isinstance(refusal, Refusal)
            assert refusal["error_code"] == "disabled"
            h.config.test_selection.enabled = True
            with principal_context(principal()):
                response = await http.post(
                    "/api/test_selection/test-select",
                    json={
                        "workspace": None,
                        "claim_epoch": 7,
                    },
                )
            assert response.status_code == 422
            assert response.json()["error_code"] == "spoofed_workspace"
    finally:
        (
            deps._orchestrator,
            deps._command_handler,
            deps._token_store,
            deps._require_session_token,
        ) = saved
