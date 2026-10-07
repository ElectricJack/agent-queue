"""Promotion protection and workflow configuration (revision 3 §3.8 / fidelity B11)."""

from __future__ import annotations

import base64
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

import pytest
import yaml
from click.testing import CliRunner

from src.commands.promote_commands import PromoteCommandsMixin
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubRepositoryBinding,
)
from src.integration.app_mode import AppModeContext, check_protection
from src.integration.promotion_steps import (
    DEFAULT_ATTESTATION,
    FlowSchema,
    promotion_rulesets,
    promotion_workflow_triggers,
    read_promotion_protection,
    validate_promotion_remote,
)
from src.integration.protection import classify

ROOT = Path(__file__).resolve().parents[1]
APP = 5075923
BINDING = GitHubRepositoryBinding(123, "fixture/promotion")
SHA = "a" * 40


def flow(two=False, custom=False):
    steps = [
        {
            "id": "release",
            "source": "dev",
            "target": "main",
            "versioning": {"kind": "semver_tag", "source": "pyproject"},
        }
    ]
    if two:
        steps[0]["source"] = "staging"
        steps.insert(
            0,
            {
                "id": "staging",
                "source": "dev",
                "target": "staging",
                "type": "continuous",
                "gate": {"approval": "none"},
            },
        )
    if custom:
        steps[-1]["versioning"] = {"kind": "custom", "tag_format": "ship-{step}-{sha12}"}
    manifest = {
        "promotion_attestation_names": [
            "Agent Queue Promotion Attestation (staging)",
            "Agent Queue Promotion Attestation (release)",
        ]
    }
    result = FlowSchema.validate(steps, default_branch="dev", manifest=manifest)
    assert result.valid
    return result.flow


class Remote:
    """GET-only repository fixture with separate effective-rule and detail reads."""

    repository = BINDING
    credential_identity = GitHubCredentialIdentity.app(APP)

    def __init__(self, steps=None, default="dev"):
        self.steps = steps if steps is not None else flow(two=True)
        self.calls = []
        self.default = default
        self.rulesets = {
            i: {
                "id": i,
                "current_user_can_bypass": "always" if rule["bypass_actors"] else "never",
                **rule,
            }
            for i, rule in enumerate(
                promotion_rulesets(self.steps, default_branch=default, app_id=APP), 1
            )
        }
        self.files = {
            path: yaml.safe_dump({"on": events}).encode()
            for path, events in promotion_workflow_triggers(
                self.steps, default_branch=default
            ).items()
        }
        self.extra_effective = {}
        self.fail = False

    async def exact_head_ref(self, branch):
        self.calls.append(("head", branch))
        return SHA

    async def paged_list(self, path, *, max_pages):
        self.calls.append(("GET", path))
        assert max_pages > 0
        if self.fail:
            raise GitHubAccessError("transient", "fixture unavailable")
        if "/rules/branches/" in path:
            branch = unquote(path.split("/rules/branches/")[1].split("?")[0])
            rules = [
                {
                    **rule,
                    "ruleset_id": item["id"],
                    "ruleset_source_type": "Repository",
                    "ruleset_source": BINDING.full_name,
                }
                for item in self.rulesets.values()
                if item["target"] == "branch"
                and item["enforcement"] == "active"
                and f"refs/heads/{branch}" in item["conditions"]["ref_name"]["include"]
                for rule in item["rules"]
            ]
            return copy.deepcopy([*rules, *self.extra_effective.get(branch, [])])
        assert path == "/repositories/123/rulesets?includes_parents=true&per_page=100"
        return [{"id": i} for i in self.rulesets]

    async def request_json(self, method, path):
        self.calls.append((method, path))
        assert method == "GET", "promotion diagnostics must never write GitHub"
        if "/rulesets/" in path:
            return copy.deepcopy(self.rulesets[int(path.rsplit("/", 1)[1])])
        if "/contents/" in path:
            name, ref = path.split("/contents/")[1].split("?ref=")
            assert ref == SHA, "workflow bytes must be pinned to the resolved SHA"
            raw = self.files.get(name)
            if raw is not None:
                return {"encoding": "base64", "content": base64.b64encode(raw).decode()}
        raise GitHubAccessError("not_found_or_hidden", "fixture file absent")


def test_ruleset_json_is_stable_and_tag_bypass_cannot_modify_tags():
    expected = json.loads((ROOT / "tests/fixtures/integration/promotion-rulesets.json").read_text())
    assert promotion_rulesets(flow(), default_branch="dev", app_id=APP) == expected
    assert len(expected) == 4
    assert [r["name"] for r in expected[:2]] == ["Train-only dev", "Promotion-only main"]
    assert expected[2]["rules"] == [{"type": "creation"}]
    assert expected[2]["bypass_actors"] == [
        {"actor_id": APP, "actor_type": "Integration", "bypass_mode": "always"}
    ]
    assert expected[3]["bypass_actors"] == []
    assert {r["type"] for r in expected[3]["rules"]} == {"update", "deletion", "non_fast_forward"}


def test_custom_tag_glob_and_every_chain_target_are_derived_from_flow():
    steps = flow(two=True, custom=True)
    rules = promotion_rulesets(steps, default_branch="dev", app_id=APP)
    assert len(rules) == 5  # Unversioned staging has no tag pair.
    assert rules[-1]["conditions"]["ref_name"]["include"] == ["refs/tags/ship-*-*"]
    triggers = promotion_workflow_triggers(steps, default_branch="dev")
    assert triggers[".github/workflows/tests.yml"]["pull_request"]["branches"] == [
        "dev",
        "staging",
        "main",
    ]
    steps[0]["type"] = "request"
    steps[0]["gate"]["approval"] = "operator"
    steps[0]["versioning"] = {
        "kind": "semver_tag",
        "source": "pyproject",
        "tag_format": "rc-{version}",
    }
    assert len(promotion_rulesets(steps, default_branch="dev", app_id=APP)) == 7


def test_shipped_ci_keeps_main_pr_and_batch_ci_and_adds_promotion_pushes():
    document = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    events = document[True]  # YAML 1.1's spelling of the unquoted Actions 'on'.
    expected = promotion_workflow_triggers(None, default_branch="main")
    assert events["pull_request"] == expected[".github/workflows/tests.yml"]["pull_request"]
    assert events["push"] == expected[".github/workflows/tests.yml"]["push"]


async def test_remote_reads_the_entire_chain_and_classifies_both_tag_rulesets():
    remote = Remote()
    result = await validate_promotion_remote(
        remote, BINDING, remote.steps, default_branch="dev", app_id=APP
    )
    assert result["warnings"] == []
    assert [b["branch"] for b in result["protection"]["branches"]] == ["dev", "staging", "main"]
    assert {b["classification"] for b in result["protection"]["branches"]} == {"attested_only"}
    assert [t["classification"] for t in result["protection"]["tags"]] == [
        "tag_create_app_only",
        "tag_immutable",
    ]
    assert all(method in {"GET", "head"} for method, _ in remote.calls)


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing-ruleset", "ruleset_missing"),
        ("wrong-check", "ruleset_check_mismatch"),
        ("wrong-app", "ruleset_check_mismatch"),
        ("disabled", "ruleset_check_mismatch"),
        ("tag-bypass", "ruleset_check_mismatch"),
        ("tag-extra-rule", "ruleset_check_mismatch"),
        ("hidden-bypass", "ruleset_unverifiable"),
        ("missing-pr-target", "workflow_trigger_missing"),
        ("missing-audit-target", "workflow_trigger_missing"),
        ("missing-promote-push", "workflow_trigger_missing"),
        ("missing-batch-push", "workflow_trigger_missing"),
        ("missing-workflow", "workflow_trigger_missing"),
        ("paths-filter", "workflow_trigger_missing"),
        ("missing-pr-type", "workflow_trigger_missing"),
        ("default-pr-types", "workflow_trigger_missing"),
        ("malformed-workflow", "workflow_unverifiable"),
    ],
)
async def test_remote_fixture_reports_each_ruleset_and_trigger_gap(change, code):
    remote = Remote()
    tests = ".github/workflows/tests.yml"
    audit = ".github/workflows/main-attestation.yml"
    if change == "missing-ruleset":
        remote.rulesets.pop(3)
    elif change in {"wrong-check", "wrong-app"}:
        check = remote.rulesets[3]["rules"][0]["parameters"]["required_status_checks"][0]
        check["context" if change == "wrong-check" else "integration_id"] = (
            DEFAULT_ATTESTATION if change == "wrong-check" else 42
        )
    elif change == "disabled":
        remote.rulesets[3]["enforcement"] = "evaluate"
    elif change == "tag-bypass":
        remote.rulesets[5]["bypass_actors"] = remote.rulesets[4]["bypass_actors"]
    elif change == "tag-extra-rule":
        remote.rulesets[4]["rules"].append({"type": "update"})
    elif change == "hidden-bypass":
        remote.rulesets[5].pop("bypass_actors")
    elif change == "missing-workflow":
        remote.files.pop(tests)
    elif change == "malformed-workflow":
        remote.files[tests] = b"on: [\n"
    else:
        path = audit if change == "missing-audit-target" else tests
        document = yaml.safe_load(remote.files[path])
        events = document["on"]
        if change == "missing-pr-target":
            events["pull_request"]["branches"].remove("staging")
        elif change == "missing-audit-target":
            events["push"]["branches"].remove("main")
        elif change in {"missing-promote-push", "missing-batch-push"}:
            events["push"]["branches"].remove(
                "aq/promote/**" if change == "missing-promote-push" else "aq/batches/**"
            )
        elif change == "paths-filter":
            events["push"]["paths"] = ["src/**"]
        elif change == "missing-pr-type":
            events["pull_request"]["types"].remove("synchronize")
        elif change == "default-pr-types":
            events["pull_request"].pop("types")
        remote.files[path] = yaml.safe_dump(document).encode()
    result = await validate_promotion_remote(
        remote, BINDING, remote.steps, default_branch="dev", app_id=APP
    )
    assert code in {w["code"] for w in result["warnings"]}
    assert all(w["layer"] == 4 for w in result["warnings"])


async def test_read_failures_are_unverifiable_not_missing_and_audit_rename_is_supported():
    remote = Remote()
    remote.files[".github/workflows/unattested-push.yml"] = remote.files.pop(
        ".github/workflows/main-attestation.yml"
    )
    result = await validate_promotion_remote(
        remote, BINDING, remote.steps, default_branch="dev", app_id=APP
    )
    assert result["warnings"] == []
    remote.fail = True
    result = await read_promotion_protection(
        remote, BINDING, remote.steps, default_branch="dev", app_id=APP
    )
    assert {w["code"] for w in result["warnings"]} == {"ruleset_unverifiable"}
    assert {b["classification"] for b in result["branches"]} == {"unverifiable"}


@pytest.mark.parametrize(
    "filters,missing",
    [
        ({"branches": ["**", "!staging"]}, ["staging"]),
        ({"branches-ignore": ["main"]}, ["main"]),
        (
            {
                "branches": [
                    "*",
                    "aq/parent/**",
                    "aq/integration/**",
                    "aq/batches/**",
                    "aq/promote/*",
                ]
            },
            ["aq/backmerge/**", "aq/promote/**"],
        ),
        ({"branches": ["**", "!aq/promote/**", "aq/promote/**"]}, []),
        ({"branches": ["**", "!aq/backmerge/**"]}, ["aq/backmerge/**"]),
    ],
)
async def test_workflow_negative_filters_and_nested_push_refs(filters, missing):
    remote = Remote()
    path = ".github/workflows/tests.yml"
    pr = {**filters, "types": ["opened", "synchronize", "reopened", "ready_for_review"]}
    remote.files[path] = yaml.safe_dump({"on": {"pull_request": pr, "push": filters}}).encode()
    result = await validate_promotion_remote(
        remote, BINDING, remote.steps, default_branch="dev", app_id=APP
    )
    assert sorted({w["branch"] for w in result["warnings"]}) == missing


def test_default_branch_attestation_never_satisfies_a_promotion_branch():
    effective = [
        {
            "type": "required_status_checks",
            "ruleset_id": 1,
            "parameters": {
                "required_status_checks": [{"context": DEFAULT_ATTESTATION, "integration_id": APP}],
            },
        }
    ]
    assert classify(effective, {1: "never"}, None, app_id=APP).classification == "attested_only"
    assert (
        classify(
            effective,
            {1: "never"},
            None,
            app_id=APP,
            attestation_name="Agent Queue Promotion Attestation (release)",
        ).classification
        == "incompatible"
    )


async def test_app_verify_reads_chain_and_tag_protection_and_warns_on_drift():
    remote = Remote()
    ctx = AppModeContext(
        project_id="p",
        repository_id="repo",
        default_branch="dev",
        binding=BINDING,
        client=remote,
        identity=remote.credential_identity,
        policy=None,
        promotion_flow=remote.steps,
    )
    result = await check_protection(ctx)
    assert result.status == "ok"
    assert len(result.observed["promotion"]["branches"]) == 3
    remote.rulesets.pop(5)
    result = await check_protection(ctx)
    assert result.status == "warn"
    assert "ruleset_missing" in result.codes


def handler(remote):
    value = PromoteCommandsMixin()
    value.db = SimpleNamespace(
        get_project=AsyncMock(return_value=SimpleNamespace(integration_repository_id="repo")),
        get_repo=AsyncMock(return_value=SimpleNamespace(project_id="p", default_branch="dev")),
    )
    value.orchestrator = SimpleNamespace(
        github_repository_binding_resolver=lambda _: BINDING, github_client_factory=lambda _: remote
    )
    value._promotion_manifest = AsyncMock(
        return_value={
            "promotion_attestation_names": [step["gate"]["attestation"] for step in remote.steps]
        }
    )
    return value


async def test_command_warnings_do_not_refuse_validation_and_rulesets_make_no_remote_writes():
    remote = Remote()
    remote.files.pop(".github/workflows/tests.yml")
    remote.rulesets.pop(5)
    value = handler(remote)
    args = {"project_id": "p", "flow": remote.steps, "use_stored": False}
    result = await value._cmd_promote_validate({**args, "remote": True})
    assert result["success"] and result["valid"]
    assert {"workflow_trigger_missing", "ruleset_missing"} <= {
        w["code"] for w in result["warnings"]
    }
    remote.calls.clear()
    result = await value._cmd_promote_rulesets(args)
    assert result["success"] and result["outcome"] == "rulesets"
    assert remote.calls == []  # Generates config; does not even read remote rulesets.
    assert len(result["rulesets"]) == 5


def test_rulesets_cli_and_contract_are_registered_as_reads(tmp_path):
    from src.cli.app import cli
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.models import SideEffectClass

    contract = CONTRACTS.get("promote_rulesets").contract
    assert contract.execution.side_effect is SideEffectClass.READ
    client = SimpleNamespace(execute=AsyncMock(return_value={"success": True, "rulesets": []}))

    @asynccontextmanager
    async def get_client():
        yield client

    path = tmp_path / "flow.yaml"
    path.write_text(yaml.safe_dump(flow()))
    with patch("src.cli.promote_rulesets._get_client", get_client):
        result = CliRunner().invoke(
            cli, ["promote", "rulesets", "--project", "p", "--file", str(path), "--json"]
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["rulesets"] == []
    client.execute.assert_awaited_once_with(
        "promote_rulesets",
        {
            "project_id": "p",
            "use_stored": False,
            "flow": flow(),
        },
    )


async def test_rulesets_scope_refuses_other_projects_before_any_provider_read():
    from src.api.auth import RequestScope
    from src.api.scope import check_command_scope
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    scope = RequestScope(kind="session", project_id="p", task_id="t", session_id="s")
    args = {"flow": flow(), "use_stored": False}
    assert check_command_scope("promote_rulesets", args, scope) is None
    assert args["project_id"] == "p"
    assert check_command_scope("promote_rulesets", {"project_id": "other"}, scope) == (
        "out of scope: project_id mismatch"
    )
    remote = Remote()
    value = handler(remote)
    principal = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL, project_id="p")
    with principal_context(principal):
        result = await value._cmd_promote_rulesets({**args, "project_id": "other"})
    assert result["outcome"] == "unauthorized"
    value.db.get_project.assert_not_awaited()
    assert remote.calls == []


@pytest.mark.parametrize("problem", ["wrong-repository", "non-app-identity"])
async def test_rulesets_need_the_designated_repository_app_identity(problem):
    remote = Remote()
    if problem == "wrong-repository":
        remote.repository = GitHubRepositoryBinding(456, "fixture/other")
    else:
        remote.credential_identity = GitHubCredentialIdentity.existing_login()
    result = await handler(remote)._cmd_promote_rulesets(
        {"project_id": "p", "flow": remote.steps, "use_stored": False}
    )
    assert not result["success"] and result["outcome"] == "not_found"
    assert remote.calls == []
