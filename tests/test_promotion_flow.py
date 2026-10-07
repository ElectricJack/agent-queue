"""Dev-branch releases §3.1/§3.11: pure validation and its command surfaces."""

from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner
from jsonschema import Draft202012Validator

from src.commands.promote_commands import PromoteCommandsMixin
from src.integration.promotion_steps import DEFAULT_ATTESTATION, FlowSchema, globs_overlap

ROOT = Path(__file__).resolve().parents[1]
TRUST = {
    "promotion_attestation_names": [
        "Agent Queue Promotion Attestation (release)",
        "Agent Queue Promotion Attestation (staging)",
        "Agent Queue Release Attestation",
        "Agent Queue Staging Attestation",
    ],
    "check_sets": {"release": ["tests"]},
    "versioning": {"reserved_globs": ["checkpoint-*"]},
}


def release():
    return {
        "id": "release",
        "source": "dev",
        "target": "main",
        "versioning": {"kind": "semver_tag", "source": "pyproject"},
        "notes": {"kind": "file_template", "path": "src/releases/notes/{version}.md"},
    }


def two_steps():
    return [
        {
            "id": "staging",
            "source": "dev",
            "target": "staging",
            "type": "continuous",
            "gate": {"approval": "none"},
            "after": {"deploy_hook": {"event_type": "promotion.staging"}},
        },
        {**release(), "source": "staging", "gate": {"checks": "manifest:release"}},
    ]


def validate(document, manifest=TRUST):
    return FlowSchema.validate(document, default_branch="dev", manifest=manifest)


@pytest.mark.parametrize("flow", [None, [], [release()], two_steps()])
@pytest.mark.parametrize("wrapped", [False, True])
def test_documented_flows_validate_without_mutating_inputs(flow, wrapped):
    document = {"promotion_flow": flow} if wrapped else flow
    before = copy.deepcopy(document)
    Draft202012Validator(FlowSchema.schema()).validate(document)
    result = validate(document)
    assert result.valid and result.layer == 3
    assert document == before
    if not flow:
        assert result.flow == flow
    else:
        assert result.flow[0]["gate"]["request_ttl"] == "7d"
        assert result.flow[0]["after"]["backmerge"] is True


def test_defaults_are_filled_including_nested_and_step_dependent_values():
    result = validate([{"id": "release", "source": "dev", "target": "main"}])
    assert result.valid
    assert result.flow == [
        {
            "id": "release",
            "source": "dev",
            "target": "main",
            "type": "request",
            "gate": {
                "checks": "inherit",
                "approval": "operator",
                "request_ttl": "7d",
                "attestation": "Agent Queue Promotion Attestation (release)",
            },
            "versioning": {"kind": "none"},
            "notes": {"kind": "none", "bootstrap_sha": None},
            "after": {"backmerge": True, "github_release": False, "deploy_hook": None},
        }
    ]
    assert validate([release()]).flow[0]["versioning"]["tag_format"] == "v{version}"


@pytest.mark.parametrize("logins", [[], ["operator"], ["Operator", "second-admin"]])
def test_operator_allowlist_is_optional_and_preserved(logins):
    step = {**release(), "gate": {"operator_logins": logins}}
    result = validate([step])
    assert result.valid
    assert result.flow[0]["gate"]["operator_logins"] == logins


@pytest.mark.parametrize("logins", ["operator", ["operator", "operator"], [""], ["a/b"], [1]])
def test_operator_allowlist_rejects_invalid_logins(logins):
    result = validate([{**release(), "gate": {"operator_logins": logins}}])
    assert not result.valid and result.layer == 1


# Each enumerated refusal in layers 1-3 has a fixture and its exact pointer.
@pytest.mark.parametrize(
    "code,path,updates",
    [
        ("flow_schema_invalid", "/0/gate/extra", {"gate": {"extra": True}}),
        ("source_not_default", "/0/source", {"source": "other"}),
        ("target_is_default", "/0/target", {"target": "dev"}),
        ("step_id_invalid", "/0/id", {"id": "Bad_Id"}),
        (
            "attestation_duplicate",
            "/0/gate/attestation",
            {"gate": {"attestation": DEFAULT_ATTESTATION}},
        ),
        ("approval_none_on_request", "/0/gate/approval", {"gate": {"approval": "none"}}),
        ("continuous_versioned", "/0/versioning/kind", {"type": "continuous"}),
        (
            "versioning_source_missing",
            "/0/versioning/source",
            {"versioning": {"kind": "semver_tag"}},
        ),
        (
            "custom_tag_format_invalid",
            "/0/versioning/tag_format",
            {"versioning": {"kind": "custom", "tag_format": "daily-{utc_date}"}},
        ),
        (
            "tag_format_collision",
            "/0/versioning/tag_format",
            {"versioning": {"kind": "custom", "tag_format": "checkpoint-{sha12}"}},
        ),
        ("notes_without_version", "/0/notes/kind", {"versioning": {"kind": "none"}}),
        (
            "github_release_without_versioning",
            "/0/after/github_release",
            {
                "versioning": {"kind": "none"},
                "notes": {"kind": "none"},
                "after": {"github_release": True},
            },
        ),
        (
            "attestation_not_in_manifest",
            "/0/gate/attestation",
            {"gate": {"attestation": "Foreign"}},
        ),
        ("check_set_unknown", "/0/gate/checks", {"gate": {"checks": "manifest:missing"}}),
    ],
)
@pytest.mark.parametrize("wrapped", [False, True])
def test_refusal_table_with_pointers(code, path, updates, wrapped):
    flow = [{**release(), **updates}]
    result = validate({"promotion_flow": flow} if wrapped else flow)
    assert not result.valid
    assert (code, ("/promotion_flow" if wrapped else "") + path) in {
        (p.code, p.pointer) for p in result.problems
    }
    assert all(p.layer == result.layer for p in result.problems)


@pytest.mark.parametrize(
    "code,path,field,value",
    [
        ("chain_broken", "/1/source", "source", "dev"),
        ("target_duplicate", "/1/target", "target", "staging"),
        ("step_id_duplicate", "/1/id", "id", "staging"),
        (
            "attestation_duplicate",
            "/1/gate/attestation",
            "gate",
            {"attestation": "Agent Queue Promotion Attestation (staging)"},
        ),
    ],
)
def test_refusals_between_steps(code, path, field, value):
    flow = two_steps()
    flow[1][field] = value
    assert (code, path) in {(p.code, p.pointer) for p in validate(flow).problems}


def test_layers_stop_before_trust_and_do_not_accept_structural_errors():
    document = {"promotion_flow": [{**release(), "source": "wrong", "oops": 1}]}
    result = validate(document, {})
    assert result.layer == 1
    assert {p.code for p in result.problems} == {"flow_schema_invalid"}
    del document["promotion_flow"][0]["oops"]
    result = validate(document, {})
    assert result.layer == 2
    assert {p.code for p in result.problems} == {"source_not_default"}
    document["promotion_flow"][0]["source"] = "dev"
    assert validate(document, {}).layer == 3
    assert not validate(document, {}).valid


def test_required_property_errors_are_unique_and_semver_formats_name_a_version():
    result = validate([{}])
    assert len(result.problems) == 3
    assert {p.pointer for p in result.problems} == {"/0/id", "/0/source", "/0/target"}
    step = release()
    step["versioning"]["tag_format"] = "{sha12}"
    assert validate([step]).layer == 1


def test_malformed_manifest_fields_do_not_trust_substrings_or_raise():
    manifest = {
        "promotion_attestation_names": "prefix Agent Queue Promotion Attestation (release) suffix",
        "check_sets": "release",
        "versioning": "bad",
    }
    step = release()
    step["gate"] = {"checks": "manifest:release"}
    result = validate([step], manifest)
    assert {p.code for p in result.problems} == {"attestation_not_in_manifest", "check_set_unknown"}


@pytest.mark.parametrize(
    "document,pointer",
    [
        ({"promotion_flow": [], "unknown~/key": 1}, "/unknown~0~1key"),
        ({"promotion_flow": [{"id": "release", "source": "dev"}]}, "/promotion_flow/0/target"),
        ({"promotion_flow": "dev -> main"}, "/promotion_flow"),
        ([{**release(), "gate": {"approval": "auto"}}], "/0/gate/approval"),
        ([{**release(), "after": {"backmerge": "yes"}}], "/0/after/backmerge"),
        ([{**release(), "notes": {"kind": "file_template"}}], "/0/notes/path"),
        ([{**release(), "target": "refs/heads/main"}], "/0/target"),
        ([{**release(), "source": "dev..branch"}], "/0/source"),
    ],
)
def test_schema_errors_report_the_supplied_document_location(document, pointer):
    result = validate(document)
    assert result.layer == 1 and not result.valid
    assert pointer in {p.pointer for p in result.problems}


@pytest.mark.parametrize(
    "format_", ["{unknown}", "{sha12!r}", "{version:02}", "{sha12", "{step}", "static"]
)
def test_custom_tag_formats_refuse_unknown_and_nonunique_placeholders(format_):
    step = {
        **release(),
        "notes": {"kind": "none"},
        "versioning": {"kind": "custom", "tag_format": format_},
    }
    assert "custom_tag_format_invalid" in {p.code for p in validate([step]).problems}


def test_continuous_custom_sha_tags_are_allowed_but_versions_need_prepare():
    step = {
        **two_steps()[0],
        "versioning": {"kind": "custom", "tag_format": "staging-{sha12}-{utc_date}-{step}"},
    }
    assert validate([step]).valid
    step["versioning"] = {
        "kind": "custom",
        "source": "package.json",
        "tag_format": "staging-{version}",
    }
    assert "continuous_versioned" in {p.code for p in validate([step]).problems}


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("v*", "v*-rc", True),
        ("v*-rc", "v*", True),
        ("staging-*", "v*", False),
        ("prefix-*suffix", "prefix-other*", True),
        ("*a", "*b", False),
        ("v[0-9]*", "v*", True),
        ("v[!0-9]*", "v[0-9]*", False),
        ("[a-c]*", "[b-d]*", True),
        ("[a-c]", "[d-f]", False),
    ],
)
def test_tag_family_overlap(left, right, expected):
    assert globs_overlap(left, right) is expected


def test_two_versioned_steps_refuse_overlapping_tag_families():
    flow = two_steps()
    flow[0]["type"] = "request"
    flow[0]["gate"] = {}
    flow[0]["versioning"] = {"kind": "custom", "source": "pyproject", "tag_format": "v{version}-rc"}
    assert "tag_format_collision" in {p.code for p in validate(flow).problems}
    flow[0]["versioning"]["tag_format"] = "staging-{version}"
    assert validate(flow).valid


def test_schema_is_published_and_cannot_be_mutated_by_a_caller():
    schema = FlowSchema.schema()
    Draft202012Validator.check_schema(schema)
    assert json.loads((ROOT / "docs/reference/promotion-flow-schema.json").read_text()) == schema
    schema.clear()
    assert FlowSchema.schema()


def handler():
    value = PromoteCommandsMixin()
    value.db = SimpleNamespace(
        get_project=AsyncMock(
            return_value=SimpleNamespace(
                integration_repository_id="repo", repo_default_branch="wrong-project-default"
            )
        ),
        get_repo=AsyncMock(return_value=SimpleNamespace(project_id="p", default_branch="dev")),
    )
    value.orchestrator = SimpleNamespace()
    value._promotion_manifest = AsyncMock(return_value=TRUST)
    return value


async def test_command_validates_using_repository_default_and_reports_remote_gap():
    value = handler()
    result = await value._cmd_promote_validate(
        {
            "project_id": "p",
            "flow": {"promotion_flow": [release()]},
            "use_stored": False,
            "remote": True,
        }
    )
    assert result["success"] and result["valid"]
    assert result["warnings"][0]["code"] == "not_implemented"
    assert result["warnings"][0]["layer"] == 4
    value._promotion_manifest.assert_awaited_once()


async def test_command_reads_stored_null_without_requiring_trust():
    value = handler()
    conn = AsyncMock()
    rows = MagicMock()
    rows.scalar_one.return_value = None
    conn.execute.return_value = rows

    @asynccontextmanager
    async def connect():
        yield conn

    value.db._engine = SimpleNamespace(connect=connect)
    result = await value._cmd_promote_validate({"project_id": "p"})
    assert result["success"] and result["flow"] is None
    value._promotion_manifest.assert_not_awaited()
    assert "promotion_flow" in str(conn.execute.call_args.args[0])


async def test_command_preserves_first_failing_layer_without_reading_manifest():
    value = handler()
    result = await value._cmd_promote_validate(
        {"project_id": "p", "use_stored": False, "flow": [{**release(), "source": "wrong"}]}
    )
    assert result["outcome"] == "invalid" and result["layer"] == 2
    value._promotion_manifest.assert_not_awaited()


async def test_command_manifest_read_errors_fail_closed():
    value = handler()
    value._promotion_manifest.side_effect = ValueError("Manifest unavailable")
    result = await value._cmd_promote_validate(
        {"project_id": "p", "use_stored": False, "flow": [release()]}
    )
    assert not result["success"] and result["layer"] == 3
    assert result["problems"][0]["code"] == "attestation_not_in_manifest"
    assert result["warnings"][0]["code"] == "trust_manifest_unavailable"


async def test_command_reads_manifest_from_exact_designated_default_sha():
    value = PromoteCommandsMixin()
    client = SimpleNamespace(exact_head_ref=AsyncMock(return_value="a" * 40))
    binding = SimpleNamespace()
    value.orchestrator = SimpleNamespace(
        github_repository_binding_resolver=lambda repo: binding,
        github_client_factory=lambda b: client,
    )
    with patch(
        "src.integration.app_mode.read_file_at", AsyncMock(return_value=json.dumps(TRUST).encode())
    ) as read:
        assert await value._promotion_manifest(SimpleNamespace(default_branch="dev")) == TRUST
    client.exact_head_ref.assert_awaited_once_with("dev")
    assert read.call_args.args[-1] == "a" * 40


async def test_worker_api_scope_admits_reads_only_in_its_project():
    from src.api.auth import RequestScope
    from src.api.scope import check_command_scope
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    scope = RequestScope(kind="session", project_id="p", task_id="task", session_id="session")
    assert check_command_scope("promote_schema", {}, scope) is None
    args = {"flow": {"promotion_flow": [release()]}, "use_stored": False}
    assert check_command_scope("promote_validate", args, scope) is None
    assert {key: args[key] for key in ("project_id", "task_id", "session_id")} == {
        "project_id": "p", "task_id": "task", "session_id": "session"
    }
    value = handler()
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, policy=DENY_ALL,
        project_id="p", task_id="task", session_id="session",
    )
    with principal_context(principal):
        result = await value._cmd_promote_validate(args)
    assert result["success"] and result["valid"] and result["project_id"] == "p"
    value.db.get_project.assert_awaited_once_with("p")
    for key in ("project_id", "task_id", "session_id"):
        assert check_command_scope("promote_validate", {key: "other"}, scope) == (
            f"out of scope: {key} mismatch"
        )


def test_contracts_are_registered_as_reads_without_changing_legacy_fields():
    from src.commands.contracts import CONTRACTS
    from src.commands.contracts.models import SideEffectClass

    for name in ("promote_schema", "promote_validate"):
        contract = CONTRACTS.get(name).contract
        assert contract.execution.side_effect is SideEffectClass.READ
    assert (
        "promotion_flow"
        not in CONTRACTS.get("integration_status").contract.execution.args_model.model_fields
    )


@pytest.mark.parametrize(
    "text,document", [("promotion_flow: []\n", {"promotion_flow": []}), ("null\n", None)]
)
def test_cli_loads_yaml_and_explicit_null(tmp_path, text, document):
    from src.cli.app import cli

    path = tmp_path / "flow.yaml"
    path.write_text(text)
    client = SimpleNamespace(
        execute=AsyncMock(return_value={"success": True, "valid": True, "flow": []})
    )

    @asynccontextmanager
    async def get_client():
        yield client

    with patch("src.cli.promote._get_client", get_client):
        result = CliRunner().invoke(
            cli,
            ["--json", "promote", "validate", "--project", "p", "--file", str(path), "--remote"],
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["valid"]
    client.execute.assert_awaited_once_with(
        "promote_validate",
        {
            "project_id": "p",
            "flow": document,
            "use_stored": False,
            "remote": True,
        },
    )


def test_cli_prints_schema_and_reports_validation_failures():
    from src.cli.app import cli

    client = SimpleNamespace(
        execute=AsyncMock(return_value={"success": True, "schema": FlowSchema.schema()})
    )

    @asynccontextmanager
    async def get_client():
        yield client

    with patch("src.cli.promote._get_client", get_client):
        result = CliRunner().invoke(cli, ["promote", "schema"])
        assert result.exit_code == 0 and json.loads(result.output) == FlowSchema.schema()
        client.execute.return_value = {
            "success": False,
            "valid": False,
            "problems": [{"code": "chain_broken", "pointer": "/1/source", "layer": 2}],
        }
        result = CliRunner().invoke(cli, ["--json", "promote", "validate", "--project", "p"])
        assert result.exit_code == 1
        assert json.loads(result.output)["data"]["problems"][0]["pointer"] == "/1/source"


def test_migration_column_is_nullable_jsonb_and_inspector_guarded():
    from sqlalchemy.dialects.postgresql import JSONB

    from src.database.tables import projects

    column = projects.c.promotion_flow
    assert isinstance(column.type, JSONB) and column.nullable and column.server_default is None
    migration = import_module("migrations.versions.a00000000085_projects_promotion_flow")
    inspector = MagicMock()
    inspector.has_table.return_value = True
    inspector.get_columns.return_value = [{"name": "id"}]
    with (
        patch.object(migration.sa, "inspect", return_value=inspector),
        patch.object(migration, "op") as op,
    ):
        migration.upgrade()
        assert op.add_column.call_args.args[0] == "projects"
        assert isinstance(op.add_column.call_args.args[1].type, JSONB)
        inspector.get_columns.return_value.append({"name": "promotion_flow"})
        migration.upgrade()
        assert op.add_column.call_count == 1
        migration.downgrade()
        op.drop_column.assert_called_once_with("projects", "promotion_flow")
        inspector.get_columns.return_value.pop()
        migration.downgrade()
        assert op.drop_column.call_count == 1


# ---------------------------------------------------------------------------
# Activation (§3.11, R16/R17): the fenced write, ``promotion_flow_in_use``,
# create-only targets, the status chain and the daemon-start re-check.
# ---------------------------------------------------------------------------


def filled(document):
    result = validate(document)
    assert result.valid, result.problems
    return result.flow


@pytest.mark.parametrize(
    "path,value",
    [
        (("source",), "elsewhere"),
        (("target",), "production"),
        (("type",), "continuous"),
        (("gate", "approval"), "requester"),
        (("gate", "checks"), "manifest:release"),
        (("versioning", "kind"), "none"),
        (("notes", "kind"), "none"),
    ],
)
def test_each_protected_field_change_is_listed(path, value):
    from src.integration.promotion_steps import protected_changes

    stored = filled([release()])
    flow = copy.deepcopy(stored)
    node = flow[0]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert [(c["step_id"], c["field"], c["pointer"], c["target"])
            for c in protected_changes(stored, flow)] == [
        ("release", path[0], f"/0/{path[0]}", "main")
    ]


def test_after_ttl_and_appended_steps_are_free_and_removal_is_not():
    from src.integration.promotion_steps import protected_changes

    stored = filled([release()])
    flow = copy.deepcopy(stored)
    flow[0]["after"]["backmerge"] = False
    flow[0]["gate"]["request_ttl"] = "1d"
    flow.append({"id": "hotfix", "source": "main", "target": "hotfix"})
    assert protected_changes(stored, flow) == []
    assert protected_changes(stored, [{"id": "release", "source": "dev", "target": "main",
                                        "versioning": stored[0]["versioning"],
                                        "notes": stored[0]["notes"]}]) == []
    assert protected_changes(stored, []) == [
        {"step_id": "release", "target": "main", "field": "id", "pointer": "/0/id"}
    ]
    assert protected_changes(None, filled([release()])) == []


def test_status_prints_the_chain_and_marks_a_failing_flow_misconfigured():
    from src.integration.promotion_steps import flow_status

    flow = filled(two_steps())
    status = flow_status(flow, default_branch="dev")
    assert status["state"] == "configured" and status["problems"] == []
    assert status["chain"] == "dev -> staging -> main"
    assert [(s["id"], s["target_ref"], s["type"], s["state"]) for s in status["steps"]] == [
        ("staging", "refs/heads/staging", "continuous", "configured"),
        ("release", "refs/heads/main", "request", "configured"),
    ]
    renamed = flow_status(flow, default_branch="trunk")
    assert renamed["state"] == "misconfigured"
    assert {s["state"] for s in renamed["steps"]} == {"misconfigured"}
    assert renamed["problems"][0]["code"] == "source_not_default"
    assert renamed["problems"][0]["pointer"] == "/0/source"
    recorded = [{"code": "check_set_unknown", "pointer": "/1/gate/checks", "layer": 3}]
    assert flow_status(flow, default_branch="dev", recorded=recorded)["problems"] == recorded
    # A default renamed back clears a recorded layer-2 finding without a restart.
    stale = [{"code": "source_not_default", "pointer": "/0/source", "layer": 2}]
    assert flow_status(flow, default_branch="dev", recorded=stale)["state"] == "configured"
    assert flow_status(None, default_branch="dev") is None
    assert flow_status([], default_branch="dev") is None


class FakeGit:
    """Remote heads by branch; create-only pushes that may lose a race."""

    def __init__(self, heads, *, ancestors=(), lost=(), unreadable=()):
        self.heads = dict(heads)
        self.ancestors = set(ancestors)
        self.lost = set(lost)
        self.unreadable = set(unreadable)
        self.pushes = []
        self.fetches = 0

    async def als_remote_refs(self, checkout, branches, *, repository_url=None):
        from src.git.manager import RemoteRefResult, RemoteRefState

        return {
            b: RemoteRefResult(RemoteRefState.ERROR, error="denied") if b in self.unreadable
            else RemoteRefResult(RemoteRefState.PRESENT, self.heads[b]) if b in self.heads
            else RemoteRefResult(RemoteRefState.ABSENT)
            for b in branches
        }

    async def afetch_origin(self, checkout, *, repository_url, all_heads=False):
        self.fetches += 1

    async def ais_ancestor(self, checkout, ancestor, descendant, *, strict=False):
        return (ancestor, descendant) in self.ancestors

    async def apush_new_refs(self, checkout, tips, *, repository_url=None):
        from src.git.manager import RemoteRefResult, RemoteRefState

        self.pushes.append(dict(tips))
        result = {}
        for branch, oid in tips.items():
            if branch in self.lost:
                result[branch] = RemoteRefResult(RemoteRefState.PRESENT, "f" * 40)
            else:
                self.heads[branch] = oid
                result[branch] = RemoteRefResult(RemoteRefState.PRESENT, oid)
        return result


DEV, MAIN = "d" * 40, "a" * 40


async def test_missing_targets_are_created_in_chain_order_at_their_source_tip():
    from src.integration.promotion_steps import create_missing_targets

    git = FakeGit({"dev": DEV, "main": MAIN}, ancestors={(MAIN, DEV)})
    assert await create_missing_targets(git, "/w", filled(two_steps())) == (
        None, {"staging": DEV})
    # staging is created at dev's tip, and main must then be its ancestor.
    assert git.pushes == [{"staging": DEV}] and git.fetches == 1
    git = FakeGit({"dev": DEV, "main": DEV})
    assert await create_missing_targets(git, "/w", filled([release()])) == (None, {})
    assert git.pushes == [] and git.fetches == 0


@pytest.mark.parametrize(
    "git,error,pointer",
    [
        (FakeGit({"dev": DEV, "main": MAIN}), "target_not_ancestor_of_source", "/0/target"),
        (FakeGit({"main": MAIN}), "source_missing_on_remote", "/0/source"),
        (FakeGit({"dev": DEV}, lost={"main"}), "target_create_failed", "/0/target"),
        (FakeGit({"dev": DEV, "main": MAIN}, unreadable={"main"}), "remote_unavailable", None),
    ],
)
async def test_layer_four_refuses_and_reports_why(git, error, pointer):
    from src.integration.promotion_steps import create_missing_targets

    before = dict(git.heads)
    refusal, created = await create_missing_targets(git, "/w", filled([release()]))
    assert refusal["error"] == error and refusal.get("pointer") == pointer
    assert created == {}
    # No existing head moves; only a refused create can have been attempted.
    assert {b: git.heads[b] for b in before} == before
    assert git.pushes in ([], [{"main": DEV}])


async def test_a_failed_create_reports_the_targets_that_did_land():
    from src.integration.promotion_steps import create_missing_targets

    git = FakeGit({"dev": DEV}, lost={"main"})
    refusal, created = await create_missing_targets(git, "/w", filled(two_steps()))
    assert refusal["error"] == "target_create_failed" and refusal["pointer"] == "/1/target"
    assert created == {"staging": DEV}


@pytest.fixture
async def flow_db(reuse_database):
    from src.models import Project, RepoConfig, RepoSourceType

    db = await reuse_database("promotion-flow.db")
    await db.create_project(Project(id="p", name="project", repo_default_branch="dev"))
    await db.create_repo(RepoConfig(
        id="repo", project_id="p", source_type=RepoSourceType.CLONE,
        url="https://github.com/acme/widgets.git", default_branch="dev"))
    await db.update_project("p", integration_repository_id="repo")
    return db


async def stored(db):
    from sqlalchemy import select

    from src.database.tables import projects

    async with db._engine.connect() as conn:
        row = (await conn.execute(select(
            projects.c.promotion_flow, projects.c.hierarchical_integration_generation
        ).where(projects.c.id == "p"))).one()
    return row.promotion_flow, row.hierarchical_integration_generation


async def configure(db, flow, *, manifest=None, dry_run=False, generation=None):
    from src.integration.records import PolicyActivation

    if generation is None:
        generation = (await stored(db))[1]
    return await PolicyActivation(db).configure(
        "p", updates={"promotion_flow": flow}, expected_generation=generation,
        reason="test", operator_id="operator", promotion_manifest=manifest,
        dry_run=dry_run)


async def open_batch(db, batch_id, target, *, lifecycle="building", intent="open",
                     trigger="promotion"):
    from sqlalchemy import insert

    from src.database.tables import integration_batches

    async with db.immediate() as conn:
        await conn.execute(insert(integration_batches).values(
            id=batch_id, project_id="p", repository_id="repo", request_id=f"req-{batch_id}",
            trigger=trigger, target_ref=f"refs/heads/{target}", intent=intent,
            source_manifest_digest="sha256:" + "3" * 64, base_sha=DEV, lifecycle=lifecycle,
            integration_branch=f"aq/promotion/{batch_id}", policy_snapshot={},
            artifact_snapshot={}, cleanup_state="pending", created_at=1.0, updated_at=1.0))


async def test_dry_run_checks_and_fills_without_writing_then_the_write_lands(flow_db):
    checked = await configure(flow_db, two_steps(), manifest=TRUST, dry_run=True)
    assert checked["outcome"] == "checked" and checked["generation"] == 0
    assert checked["updates"]["promotion_flow"] == filled(two_steps())
    assert await stored(flow_db) == (None, 0)
    result = await configure(flow_db, two_steps(), manifest=TRUST)
    assert result["outcome"] == "configured" and result["fields"] == ["promotion_flow"]
    assert await stored(flow_db) == (filled(two_steps()), 1)
    cleared = await configure(flow_db, None)
    assert cleared["outcome"] == "configured" and await stored(flow_db) == (None, 2)


@pytest.mark.parametrize(
    "flow,manifest,error",
    [
        ([{**release(), "source": "main"}], TRUST, "source_not_default"),
        ([{**release(), "target": "dev"}], None, "target_is_default"),
        (two_steps(), {**TRUST, "check_sets": {}}, "check_set_unknown"),
    ],
)
async def test_any_failing_layer_refuses_and_writes_nothing(flow_db, flow, manifest, error):
    for dry_run in (True, False):
        result = await configure(flow_db, flow, manifest=manifest, dry_run=dry_run)
        assert result["outcome"] == "invalid" and result["error"] == error
        assert result["problems"][0]["code"] == error
    assert await stored(flow_db) == (None, 0)


async def test_stale_generation_refuses_the_dry_run_and_the_write(flow_db):
    for dry_run in (True, False):
        stale = await configure(flow_db, [release()], generation=5, dry_run=dry_run)
        assert stale["outcome"] == "stale"
    assert await stored(flow_db) == (None, 0)


async def test_open_promotion_freezes_its_step_but_not_after_or_ttl(flow_db):
    assert (await configure(flow_db, [release()]))["outcome"] == "configured"
    await open_batch(flow_db, "b1", "main")
    changed = filled([release()])
    changed[0]["versioning"]["tag_format"] = "release-v{version}"
    refused = await configure(flow_db, changed)
    assert refused["outcome"] == "in_use" and refused["error"] == "promotion_flow_in_use"
    assert (refused["step_id"], refused["field"], refused["pointer"]) == (
        "release", "versioning", "/0/versioning")
    assert (refused["batch_id"], refused["request_id"]) == ("b1", "req-b1")
    removed = await configure(flow_db, None)
    assert removed["outcome"] == "in_use" and removed["field"] == "id"
    free = filled([release()])
    free[0]["after"]["backmerge"] = False
    free[0]["gate"]["request_ttl"] = "1d"
    assert (await configure(flow_db, free))["outcome"] == "configured"
    assert (await stored(flow_db))[0] == free


@pytest.mark.parametrize(
    "lifecycle,intent,trigger",
    [("promoted", "open", "promotion"), ("building", "aborted", "promotion"),
     ("building", "open", "schedule")],
)
async def test_closed_or_train_batches_do_not_freeze_a_step(flow_db, lifecycle, intent, trigger):
    assert (await configure(flow_db, [release()]))["outcome"] == "configured"
    await open_batch(flow_db, "b1", "main", lifecycle=lifecycle, intent=intent, trigger=trigger)
    changed = filled([release()])
    changed[0]["versioning"]["tag_format"] = "release-v{version}"
    assert (await configure(flow_db, changed))["outcome"] == "configured"


async def test_live_work_on_a_new_target_refuses_activation(flow_db):
    from src.integration.records import PolicyActivation

    await open_batch(flow_db, "b1", "staging", trigger="schedule")
    result = await configure(flow_db, two_steps(), dry_run=True)
    assert result["outcome"] == "busy" and result["error"] == "repository_busy"
    assert result["targets"] == ["main", "staging"]
    assert await stored(flow_db) == (None, 0)
    # F10: an open train batch now holds the repository on the strict path too.
    assert await PolicyActivation(flow_db).has_active_work("p") is True


async def test_busy_check_names_only_new_targets(flow_db):
    assert (await configure(flow_db, [release()]))["outcome"] == "configured"
    await open_batch(flow_db, "b1", "main", trigger="schedule")
    appended = [*filled([release()]), {"id": "hotfix", "source": "main", "target": "hotfix"}]
    assert (await configure(flow_db, appended))["outcome"] == "configured"


def project_handler(db, git=None, *, recorded=None):
    from src.commands.project_commands import ProjectCommandsMixin

    class Handler(ProjectCommandsMixin, PromoteCommandsMixin):
        pass

    value = Handler()
    value.db = db
    value.orchestrator = SimpleNamespace(
        git=git or FakeGit({"dev": DEV, "main": DEV}),
        promotion_flow_problems=recorded if recorded is not None else {},
    )
    value._promotion_manifest = AsyncMock(return_value=TRUST)
    return value


def edit(flow, generation=0):
    return {"project_id": "p", "promotion_flow": flow,
            "expected_integration_generation": generation, "reason": "release flow"}


async def test_handler_activates_creates_targets_and_clears_the_start_finding(flow_db):
    git = FakeGit({"dev": DEV})
    recorded = {"p": [{"code": "source_not_default"}], "other": [{"code": "chain_broken"}]}
    value = project_handler(flow_db, git, recorded=recorded)
    with patch.object(flow_db, "get_project_workspace_path", AsyncMock(return_value="/w")):
        result = await value._cmd_edit_project(edit(two_steps()))
    assert result["success"], result
    assert result["outcome"] == "configured" and result["generation"] == 1
    assert git.pushes == [{"staging": DEV, "main": DEV}]
    assert result["created_targets"] == {"staging": DEV, "main": DEV}
    assert (await stored(flow_db))[0] == filled(two_steps())
    assert recorded == {"other": [{"code": "chain_broken"}]}
    value._promotion_manifest.assert_awaited_once()


async def test_handler_refusals_write_nothing(flow_db):
    from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
    from src.profiles.capabilities import DENY_ALL

    value = project_handler(flow_db)
    session = ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL,
                                 session_id="s", project_id="p")
    with principal_context(session):
        refused = await value._cmd_edit_project(edit([release()]))
    assert refused["success"] is False and refused["error"] == "local_operator_only"
    invalid = await value._cmd_edit_project(edit([{**release(), "source": "main"}]))
    assert invalid["outcome"] == "invalid" and invalid["error"] == "source_not_default"
    value._promotion_manifest.assert_not_awaited()
    value._promotion_manifest.side_effect = RuntimeError("manifest unreadable")
    blocked = await value._cmd_edit_project(edit([release()]))
    assert blocked["outcome"] == "blocked" and blocked["error"] == "trust_manifest_unavailable"
    value._promotion_manifest.side_effect = None
    no_workspace = await value._cmd_edit_project(edit([release()]))
    assert no_workspace["error"] == "workspace_unavailable"
    stale = await value._cmd_edit_project(edit([release()], generation=3))
    assert stale["success"] is False and stale["error"] == "integration_generation_stale"
    with patch.object(flow_db, "get_repo", AsyncMock(return_value=None)):
        unbound = await value._cmd_edit_project(edit([release()]))
    assert unbound["outcome"] == "blocked"
    assert unbound["error"] == "integration_repository_required"
    assert await stored(flow_db) == (None, 0)


async def test_handler_pushes_outside_the_fence_and_the_write_rechecks_it(flow_db):
    git = FakeGit({"dev": DEV})
    value = project_handler(flow_db, git)
    real_push = git.apush_new_refs

    async def push_while_a_racing_write_lands(*args, **kwargs):
        # A database lock held across the push would deadlock this write.
        assert (await configure(flow_db, [release()]))["outcome"] == "configured"
        return await real_push(*args, **kwargs)

    git.apush_new_refs = push_while_a_racing_write_lands
    with patch.object(flow_db, "get_project_workspace_path", AsyncMock(return_value="/w")):
        result = await value._cmd_edit_project(edit(two_steps()))
    assert result["success"] is False and result["error"] == "integration_generation_stale"
    assert git.pushes == [{"staging": DEV, "main": DEV}]
    assert result["created_targets"] == {"staging": DEV, "main": DEV}
    assert await stored(flow_db) == (filled([release()]), 1)


async def test_daemon_start_recheck_marks_a_renamed_default_and_a_changed_manifest(flow_db):
    from sqlalchemy import update

    from src.database.tables import repos
    from src.integration.promotion_steps import recheck_stored_flows
    from src.integration.status import IntegrationStatusService

    value = project_handler(flow_db)
    with patch.object(flow_db, "get_project_workspace_path", AsyncMock(return_value="/w")):
        assert (await value._cmd_edit_project(edit(two_steps())))["success"]

    async def check():
        return await recheck_stored_flows(
            flow_db, lambda pid: value._cmd_promote_validate({"project_id": pid}))

    assert await check() == {}
    status = (await IntegrationStatusService(flow_db).status("p"))["promotion_flow"]
    assert status["state"] == "configured" and status["chain"] == "dev -> staging -> main"
    # The trust manifest at the default branch dropped the check set.
    value._promotion_manifest.return_value = {**TRUST, "check_sets": {}}
    found = await check()
    assert found["p"][0]["code"] == "check_set_unknown"
    assert found["p"][0]["pointer"] == "/1/gate/checks"
    status = (await IntegrationStatusService(flow_db, flow_problems=found).status("p"))
    assert status["promotion_flow"]["state"] == "misconfigured"
    assert {s["state"] for s in status["promotion_flow"]["steps"]} == {"misconfigured"}
    # An unreadable manifest alone is not a manifest change.
    value._promotion_manifest.side_effect = RuntimeError("offline")
    assert await check() == {}
    value._promotion_manifest.side_effect = None
    value._promotion_manifest.return_value = TRUST
    async with flow_db.immediate() as conn:
        await conn.execute(update(repos).where(repos.c.id == "repo").values(
            default_branch="trunk"))
    found = await check()
    assert (found["p"][0]["code"], found["p"][0]["pointer"]) == ("source_not_default", "/0/source")
    # Status re-runs layers 1-2 on read, so the rename shows without a restart.
    status = (await IntegrationStatusService(flow_db).status("p"))["promotion_flow"]
    assert status["state"] == "misconfigured" and status["problems"][0]["code"] == (
        "source_not_default")
    assert (await stored(flow_db))[0] == filled(two_steps())


async def test_daemon_start_recheck_survives_a_failing_project(flow_db):
    from src.integration.promotion_steps import recheck_stored_flows

    assert (await configure(flow_db, [release()]))["outcome"] == "configured"
    validate_ = AsyncMock(side_effect=RuntimeError("boom"))
    found = await recheck_stored_flows(flow_db, validate_)
    assert found == {"p": [{"code": "flow_check_failed", "pointer": "",
                            "message": "RuntimeError: boom", "layer": 0}]}
    validate_.assert_awaited_once_with("p")


async def test_daemon_start_recheck_drops_a_flow_replaced_while_it_ran(flow_db):
    from src.integration.promotion_steps import recheck_stored_flows

    assert (await configure(flow_db, [release()]))["outcome"] == "configured"

    async def validate_while_an_activation_lands(project_id):
        assert (await configure(flow_db, two_steps()))["outcome"] == "configured"
        return {"outcome": "invalid", "problems": [{"code": "check_set_unknown", "layer": 3}]}

    assert await recheck_stored_flows(flow_db, validate_while_an_activation_lands) == {}


async def test_train_keeps_the_default_while_the_flow_is_misconfigured(flow_db):
    from sqlalchemy import update

    from src.commands.contracts.integration import IntegrationStatusValue
    from src.database.tables import projects, repos
    from src.integration.status import IntegrationStatusService
    from src.integration.train_sources import DatabaseTargets

    assert (await configure(flow_db, two_steps()))["outcome"] == "configured"
    async with flow_db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_mode="train"))
        await conn.execute(update(repos).where(repos.c.id == "repo").values(
            default_branch="trunk"))
    targets = await DatabaseTargets(flow_db).targets(0.0)
    assert [(t.project_id, t.target_ref, t.kind) for t in targets] == [
        ("p", "refs/heads/trunk", "root")]
    status = await IntegrationStatusService(flow_db, git_first="active").status("p")
    assert status["projection_kind"] == "train"
    assert status["promotion_flow"]["state"] == "misconfigured"
    assert status["promotion_flow"]["problems"][0]["pointer"] == "/0/source"
    assert "promotion_flow" in IntegrationStatusValue.model_fields


@pytest.mark.parametrize("document", [42, "invalid", {"unexpected": True}, [None]])
async def test_malformed_stored_flow_reports_status_and_keeps_the_default(flow_db, document):
    from sqlalchemy import update

    from src.database.tables import projects
    from src.integration.status import IntegrationStatusService
    from src.integration.train_sources import DatabaseTargets

    async with flow_db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            promotion_flow=document, hierarchical_integration_mode="train"))
    targets = await DatabaseTargets(flow_db).targets(0.0)
    assert [(t.target_ref, t.kind) for t in targets] == [("refs/heads/dev", "root")]
    status = await IntegrationStatusService(flow_db, git_first="active").status("p")
    assert status["promotion_flow"]["state"] == "misconfigured"
    assert status["promotion_flow"]["problems"][0]["code"] == "flow_schema_invalid"


async def test_discovery_holds_manifest_invalid_flow_and_sees_cleared_findings(flow_db):
    from sqlalchemy import update

    from src.database.tables import projects
    from src.integration.train_sources import DatabaseTargets

    assert (await configure(flow_db, two_steps(), manifest=TRUST))["outcome"] == "configured"
    async with flow_db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "p").values(
            hierarchical_integration_mode="train"))
    findings = {"p": [{"code": "check_set_unknown", "pointer": "/1/gate/checks", "layer": 3}]}
    discovery = DatabaseTargets(flow_db, flow_problems=lambda: findings)
    assert [(t.target_ref, t.kind) for t in await discovery.targets(0.0)] == [
        ("refs/heads/dev", "root")]
    findings = {}
    assert {t.target_ref for t in await discovery.targets(0.0)} == {
        "refs/heads/dev", "refs/heads/staging", "refs/heads/main"}


def project_set_client(result):
    client = SimpleNamespace(execute=AsyncMock(return_value=result))

    @asynccontextmanager
    async def get_client(*_args):
        yield client

    return client, get_client


def test_project_set_promotion_flow_loads_the_file_under_the_generation_fence(tmp_path):
    from src.cli.app import cli

    path = tmp_path / "flow.yaml"
    path.write_text("promotion_flow:\n  - id: release\n    source: dev\n    target: main\n")
    client, get_client = project_set_client(
        {"success": True, "outcome": "configured", "generation": 1})
    argv = ["--json", "project", "set", "p", "promotion-flow", str(path),
            "--expected-integration-generation", "0", "--reason", "release flow"]
    with patch("src.cli.projects._get_client", get_client):
        result = CliRunner().invoke(cli, argv)
        assert result.exit_code == 0, result.output
        client.execute.assert_awaited_once_with("edit_project", {
            "project_id": "p",
            "promotion_flow": {"promotion_flow": [
                {"id": "release", "source": "dev", "target": "main"}]},
            "expected_integration_generation": 0, "reason": "release flow"})
        client.execute.reset_mock()
        cleared = CliRunner().invoke(cli, argv[:5] + ["clear"] + argv[6:])
        assert cleared.exit_code == 0, cleared.output
        assert client.execute.await_args.args[1]["promotion_flow"] is None
        unfenced = CliRunner().invoke(cli, argv[:7])
        assert unfenced.exit_code == 2 and "--expected-integration-generation" in unfenced.output


def test_project_set_promotion_flow_exits_nonzero_on_a_refusal(tmp_path):
    from src.cli.app import cli

    path = tmp_path / "flow.yaml"
    path.write_text("promotion_flow: [\n")
    client, get_client = project_set_client({
        "success": False, "outcome": "in_use", "error": "promotion_flow_in_use"})
    argv = ["--json", "project", "set", "p", "promotion-flow", str(path),
            "--expected-integration-generation", "0"]
    with patch("src.cli.projects._get_client", get_client):
        unreadable = CliRunner().invoke(cli, argv)
        assert unreadable.exit_code == 1
        assert json.loads(unreadable.output)["error"]["code"] == "flow_schema_invalid"
        client.execute.assert_not_awaited()
        path.write_text("promotion_flow: []\n")
        refused = CliRunner().invoke(cli, argv)
    assert refused.exit_code == 1
    assert "promotion_flow_in_use" in refused.output


async def test_doctor_names_the_first_failing_pointer_per_project(flow_db):
    from src.doctor import default_registry
    from src.doctor.integration_checks import _BY_ID
    from src.doctor.models import DoctorContext, Severity

    check = _BY_ID["integration.promotion_flow"]
    assert "integration.promotion_flow" in {c.id for c in default_registry().checks()}
    value = project_handler(flow_db)

    async def execute(command, args):
        assert command == "promote_validate"
        return await value._cmd_promote_validate(args)

    ctx = DoctorContext(config=SimpleNamespace(), db=flow_db,
                        handler=SimpleNamespace(execute=execute))
    assert (await check.run(ctx)).severity == Severity.OK
    assert (await configure(flow_db, two_steps()))["outcome"] == "configured"
    assert (await check.run(ctx)).severity == Severity.OK
    value._promotion_manifest.return_value = {**TRUST, "check_sets": {}}
    result = await check.run(ctx)
    assert result.severity == Severity.ERROR
    assert "p: check_set_unknown at '/1/gate/checks'" in result.detail
    assert result.data["projects"][0]["pointer"] == "/1/gate/checks"
    assert (await check.run(DoctorContext(config=SimpleNamespace()))).severity == Severity.INFO


async def test_daemon_start_records_findings_and_logs_the_pointer(flow_db, caplog):
    from src.orchestrator.core import Orchestrator

    assert (await configure(flow_db, two_steps()))["outcome"] == "configured"
    value = project_handler(flow_db)
    value._promotion_manifest.return_value = {**TRUST, "check_sets": {}}

    async def execute(command, args):
        return await value._cmd_promote_validate(args)

    orchestrator = SimpleNamespace(_command_handler=SimpleNamespace(execute=execute),
                                   db=flow_db, promotion_flow_problems={})
    with caplog.at_level("WARNING", logger="src.orchestrator.core"):
        await Orchestrator._recheck_promotion_flows(orchestrator)
    assert orchestrator.promotion_flow_problems["p"][0]["code"] == "check_set_unknown"
    assert "check_set_unknown at '/1/gate/checks'" in caplog.text
    assert (await stored(flow_db))[0] == filled(two_steps())


async def test_status_names_the_ci_source_per_target_kind(flow_db):
    from sqlalchemy import update

    from src.database.tables import projects
    from src.integration.status import IntegrationStatusService
    from tests.test_integration_service import _minimal_policy_values

    async def both():
        control = await IntegrationStatusService(flow_db).status("p")
        train = await IntegrationStatusService(flow_db, git_first="active").train_status("p")
        assert control["ci_source"] == train["ci_source"]
        return train["ci_source"]

    async def store(policy):
        async with flow_db._engine.begin() as conn:
            await conn.execute(update(projects).where(projects.c.id == "p")
                               .values(hierarchical_integration_policy=policy))

    hosted = {"root": "hosted", "epic": "hosted", "promotion": "hosted"}
    assert await both() == {**hosted, "origin": "default"}
    await store({**_minimal_policy_values(), "ci": {
        "source": "hybrid", "epic": "local", "commands": {"unit": "aq test tests/test_x.py"}}})
    assert await both() == {
        "root": "hybrid", "epic": "local", "promotion": "hybrid", "origin": "policy"}
    pin = _minimal_policy_values()["root"]["route"]
    await store({"development": {"route": pin}})
    assert (await both())["origin"] == "development"
