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


def test_worker_api_scope_admits_reads_only_in_its_project():
    from src.api.auth import RequestScope
    from src.api.scope import check_command_scope

    scope = RequestScope(kind="session", project_id="p", task_id="task", session_id="session")
    assert check_command_scope("promote_schema", {}, scope) is None
    assert check_command_scope("promote_validate", {"project_id": "p"}, scope) is None
    assert check_command_scope("promote_validate", {"project_id": "other"}, scope)


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
    migration = import_module("migrations.versions.a00000000080_projects_promotion_flow")
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
