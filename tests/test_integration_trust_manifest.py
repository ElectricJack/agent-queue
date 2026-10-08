"""The App-mode trust manifest: shared builder, daemon command and CLI (spec §6.1)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from src.api.auth import RequestScope
from src.api.scope import check_command_scope
from src.commands.contracts.models import SideEffectClass
from src.commands.contracts.registry import ContractRegistry
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubRepositoryBinding,
)
from src.integration import trust_manifest
from src.integration.ci import IntegrationTrustManifest
from src.profiles.capabilities import DENY_ALL

REPO = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO / "docs/config/agent-queue-train-policy.json"
EXAMPLE_PATH = REPO / ".github/agent-queue-integration.example.json"
MANIFEST_PATH = REPO / trust_manifest.TRUST_MANIFEST_PATH
SHA = "a" * 40
FLOW = [
    {"gate": {"attestation": "Agent Queue Promotion Attestation (staging)"}},
    {"gate": {"attestation": "Agent Queue Promotion Attestation (release)"}},
]

#: Spec §9.2 step 3: agent-queue's committed manifest, the text
#: ``aq integration trust-manifest agent-queue --policy
#: docs/config/agent-queue-train-policy.json --repository-id agent-queue2
#: --write .github/agent-queue-integration.json`` writes.  The first builder
#: test pins it to the reviewed policy, so a check-set rotation (spec §9.5) is
#: the policy and a regenerated manifest, with no copy here to edit.
AGENT_QUEUE_MANIFEST = MANIFEST_PATH.read_text()


def _policy() -> dict:
    return json.loads(POLICY_PATH.read_text())


def _agent_queue_manifest(policy=None, *, promotion_flow=FLOW) -> dict:
    policy = policy or _policy()
    return trust_manifest.manifest_for_policy(
        policy,
        canonical_repository_id="agent-queue2",
        repository_id=1160639300,
        full_name="ElectricJack/agent-queue",
        attestation_app_id=5075923,
        promotion_flow=promotion_flow,
        check_sets={"promotion-source-audit": [
            "unattested-ci / " + name for name in policy["root"]["required_checks"]["names"]
        ]},
    )


def _with_producers(parent: str, root: str) -> dict:
    policy = _policy()
    policy["parent"]["required_checks"]["producer_id"] = parent
    policy["root"]["required_checks"]["producer_id"] = root
    return policy


# ---------------------------------------------------------------------------
# The pure builder
# ---------------------------------------------------------------------------


def test_the_committed_manifest_is_the_builder_output_for_the_reviewed_policy():
    manifest = _agent_queue_manifest()
    committed = IntegrationTrustManifest.model_validate_json(AGENT_QUEUE_MANIFEST)

    # A policy check-set change without --write regenerating the file fails here.
    assert AGENT_QUEUE_MANIFEST == trust_manifest.canonical_text(manifest), (
        "regenerate with: aq integration trust-manifest agent-queue --policy "
        "docs/config/agent-queue-train-policy.json --repository-id agent-queue2 "
        "--write .github/agent-queue-integration.json"
    )
    assert committed.model_dump(mode="json", by_alias=True, exclude_unset=True) == manifest
    assert trust_manifest.compare(manifest, AGENT_QUEUE_MANIFEST).status == "ok"
    # Policy order, not sorted order: the check-name comparison is ordered.
    assert manifest["required_checks"]["names"] == _policy()["root"]["required_checks"]["names"]


def test_promotion_trust_is_explicit_without_changing_existing_manifests():
    original = trust_manifest.manifest_for_policy(
        _policy(), canonical_repository_id="agent-queue2", repository_id=1160639300,
        full_name="ElectricJack/agent-queue", attestation_app_id=5075923,
    )
    assert "promotion_attestation_names" not in original
    assert "check_sets" not in original
    original_text = trust_manifest.canonical_text(original)
    assert trust_manifest.compare(original, original_text).status == "ok"
    extended = trust_manifest.manifest_for_policy(
        _policy(), canonical_repository_id="agent-queue2", repository_id=1160639300,
        full_name="ElectricJack/agent-queue", attestation_app_id=5075923,
        promotion_flow=FLOW, check_sets={"release": ["release-unit"]},
    )
    assert extended["promotion_attestation_names"] == [step["gate"]["attestation"] for step in FLOW]
    assert extended["check_sets"] == {"release": ["release-unit"]}
    result = trust_manifest.compare(extended, original_text)
    assert result.status == "fail"
    assert {diff.field for diff in result.diff} == {"promotion_attestation_names", "check_sets"}
    assert all(not diff.committed_present for diff in result.diff)


def test_canonical_text_sorts_keys_like_the_example_and_keeps_list_order():
    text = trust_manifest.canonical_text({"b": [3, 1, 2], "a": {"z": 1, "y": 2}})

    assert text == '{\n  "a": {\n    "y": 2,\n    "z": 1\n  },\n  "b": [\n    3,\n    1,\n    2\n  ]\n}\n'
    example = json.loads(EXAMPLE_PATH.read_text())
    assert trust_manifest.canonical_text(example) == EXAMPLE_PATH.read_text()




@pytest.mark.parametrize(
    ("parent", "root", "code"),
    [
        ("github-actions", "15368", "ci_producer_not_numeric"),
        ("15368", "github-actions", "ci_producer_not_numeric"),
        ("015368", "15368", "ci_producer_not_numeric"),
        ("15368", "4242", "ci_producer_mismatch"),
    ],
)
def test_the_policy_producer_must_be_one_numeric_app_id(parent, root, code):
    with pytest.raises(trust_manifest.TrustManifestRefusal) as refused:
        _agent_queue_manifest(_with_producers(parent, root))

    assert refused.value.code == code


def test_an_app_that_is_its_own_producer_is_refused():
    with pytest.raises(trust_manifest.TrustManifestRefusal) as refused:
        trust_manifest.manifest_for_policy(
            _policy(),
            canonical_repository_id="agent-queue2",
            repository_id=1160639300,
            full_name="ElectricJack/agent-queue",
            attestation_app_id=15368,
        )

    assert refused.value.code == "trust_manifest_invalid"


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def _committed(**changes) -> dict:
    committed = json.loads(AGENT_QUEUE_MANIFEST)
    for dotted, value in changes.items():
        target = committed
        *parents, leaf = dotted.split("__")
        for part in parents:
            target = target[part]
        target[leaf] = value
    return committed


def test_an_equal_canonical_copy_is_ok():
    result = trust_manifest.compare(_agent_queue_manifest(), AGENT_QUEUE_MANIFEST)

    assert result.as_dict() == {
        "present": True,
        "valid": True,
        "identity_equal": True,
        "check_set_equal": True,
        "canonical": True,
        "status": "ok",
        "code": None,
        "warnings": [],
        "diff": [],
        "error": None,
    }


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"repository_id": 1384141153}, "repository_id"),
        ({"full_name": "ElectricJack/other"}, "full_name"),
        ({"canonical_repository_id": "agent-queue"}, "canonical_repository_id"),
        ({"attestation_app_id": 5052310}, "attestation_app_id"),
        ({"ci_producer_app_id": 4242}, "ci_producer_app_id"),
    ],
)
def test_identity_drift_fails_and_names_the_field(changes, field):
    committed = trust_manifest.canonical_text(_committed(**changes))

    result = trust_manifest.compare(_agent_queue_manifest(), committed)

    assert result.status == "fail"
    assert result.code == "trust_manifest_mismatch"
    assert not result.identity_equal and result.check_set_equal
    assert [(item.field, item.kind) for item in result.diff] == [(field, "identity")]
    assert result.diff[0].committed == changes[field]


def test_a_string_where_the_schema_needs_an_integer_is_an_identity_difference():
    committed = trust_manifest.canonical_text(_committed(ci_producer_app_id="15368"))

    result = trust_manifest.compare(_agent_queue_manifest(), committed)

    assert not result.valid and result.code == "trust_manifest_unavailable"
    assert [item.field for item in result.diff] == ["ci_producer_app_id"]


def test_check_set_only_drift_is_a_warning():
    committed = trust_manifest.canonical_text(
        _committed(required_checks__version="tests-yml-v2", required_checks__names=["Tests"])
    )

    result = trust_manifest.compare(_agent_queue_manifest(), committed)

    assert result.identity_equal and not result.check_set_equal
    assert result.status == "warn" and result.code is None
    assert result.warnings == ("trust_manifest_check_set_differs",)
    assert {item.field for item in result.diff} == {
        "required_checks.version", "required_checks.names",
    }


def test_reordered_check_names_are_a_check_set_difference():
    names = list(reversed(_policy()["root"]["required_checks"]["names"]))
    committed = trust_manifest.canonical_text(_committed(required_checks__names=names))

    result = trust_manifest.compare(_agent_queue_manifest(), committed)

    assert result.identity_equal and not result.check_set_equal


def test_noncanonical_formatting_is_a_warning():
    committed = json.dumps(json.loads(AGENT_QUEUE_MANIFEST), indent=4)

    result = trust_manifest.compare(_agent_queue_manifest(), committed)

    assert result.identity_equal and result.check_set_equal and not result.canonical
    assert result.warnings == ("trust_manifest_noncanonical",)


@pytest.mark.parametrize(
    ("committed", "error"),
    [
        (None, None),
        ("{not json", "not a JSON document"),
        ('{"schema": "a", "schema": "b"}', "duplicate field"),
        ("[]", "not a JSON object"),
        ("x" * (trust_manifest.MAX_MANIFEST_BYTES + 1), "larger than"),
    ],
)
def test_a_missing_or_unparseable_copy_is_unavailable(committed, error):
    result = trust_manifest.compare(_agent_queue_manifest(), committed)

    assert result.status == "fail"
    assert result.code == "trust_manifest_unavailable"
    assert result.present is (committed is not None)
    assert (result.error is None) if error is None else (error in result.error)


def test_an_unexpected_field_fails_like_the_daemon_parser():
    committed = _committed()
    committed["bypass"] = True
    committed["required_checks"]["extra"] = 1

    result = trust_manifest.compare(_agent_queue_manifest(), json.dumps(committed))

    assert not result.valid and result.status == "fail"
    assert {(item.field, item.kind) for item in result.diff} == {
        ("bypass", "unexpected"), ("required_checks.extra", "unexpected"),
    }


# ---------------------------------------------------------------------------
# The daemon command
# ---------------------------------------------------------------------------

BINDING = GitHubRepositoryBinding(1160639300, "ElectricJack/agent-queue")


class _Client:
    repository = BINDING

    def __init__(self, identity=None, committed: str | None = AGENT_QUEUE_MANIFEST,
                 default_branch: str = "main"):
        self.credential_identity = identity or GitHubCredentialIdentity.app(5075923, 164874645)
        self.committed = committed
        self.paths: list[str] = []
        self.default_branch = default_branch

    async def exact_head_ref(self, branch):
        assert branch == self.default_branch
        return SHA

    async def request_json(self, method, path):
        assert method == "GET"
        self.paths.append(path)
        if self.committed is None:
            raise GitHubAccessError("not_found_or_hidden", "GitHub request failed")
        return {
            "encoding": "base64",
            "content": base64.encodebytes(self.committed.encode()).decode(),
        }


def _handler(client, *, bound_policy=None, designated="agent-queue2"):
    handler = IntegrationCommandsMixin()
    project = SimpleNamespace(
        id="agent-queue",
        hierarchical_integration_policy=bound_policy,
        integration_repository_id=designated,
        promotion_flow=FLOW,
    )
    repository = SimpleNamespace(
        id="agent-queue2",
        project_id="agent-queue",
        default_branch="main",
        url="https://github.com/ElectricJack/agent-queue.git",
    )
    handler.db = SimpleNamespace(
        get_project=AsyncMock(return_value=project),
        get_repo=AsyncMock(side_effect=lambda rid: repository if rid == "agent-queue2" else None),
        get_session=AsyncMock(return_value=None),
    )
    handler.orchestrator = SimpleNamespace(
        github_repository_binding_resolver=AsyncMock(return_value=BINDING),
        github_client_factory=lambda binding: client,
    )
    return handler


async def test_command_renders_from_the_supplied_policy_and_compares_the_committed_copy():
    client = _Client()
    handler = _handler(client)

    result = await handler._cmd_integration_trust_manifest(
        {"project_id": "agent-queue", "policy": _policy(), "repository_id": "agent-queue2"}
    )

    assert result["outcome"] == "manifest" and result["success"] is True
    assert result["text"] == AGENT_QUEUE_MANIFEST
    assert result["manifest"] == json.loads(AGENT_QUEUE_MANIFEST)
    assert result["sha256"] == trust_manifest.text_sha256(AGENT_QUEUE_MANIFEST)
    assert result["policy_source"] == "argument"
    assert (result["github_repository_id"], result["full_name"], result["attestation_app_id"]) == (
        1160639300, "ElectricJack/agent-queue", 5075923,
    )
    committed = result["committed"]
    assert committed["sha"] == SHA and committed["ref"] == "main"
    assert committed["present"] and committed["identity_equal"] and committed["check_set_equal"]
    # Read through the App, bound to the numeric id, at the exact SHA.
    assert client.paths == [
        f"/repositories/1160639300/contents/.github/agent-queue-integration.json?ref={SHA}"
    ]


async def test_command_reads_stored_flow_when_project_dto_omits_it(reuse_database):
    from sqlalchemy import update
    from src.database.tables import projects
    from src.models import Project, RepoConfig, RepoSourceType

    db = await reuse_database("trust-manifest-promotion-flow.db")
    await db.create_project(Project(id="agent-queue", name="Agent Queue",
                                   hierarchical_integration_policy=_policy()))
    await db.create_repo(RepoConfig(id="agent-queue2", project_id="agent-queue",
        source_type=RepoSourceType.CLONE, url="https://github.com/ElectricJack/agent-queue.git",
        default_branch="dev"))
    steps = [
        {"id": "staging", "source": "dev", "target": "staging", **FLOW[0]},
        {"id": "release", "source": "staging", "target": "main", **FLOW[1]},
    ]
    async with db.immediate() as conn:
        await conn.execute(update(projects).where(projects.c.id == "agent-queue").values(
            integration_repository_id="agent-queue2", promotion_flow=steps,
        ))
    assert not hasattr(await db.get_project("agent-queue"), "promotion_flow")
    value = _handler(_Client(default_branch="dev"), bound_policy=_policy())
    value.db = db
    result = await value._cmd_integration_trust_manifest({"project_id": "agent-queue"})
    assert result["success"]
    assert result["text"] == AGENT_QUEUE_MANIFEST
    assert result["manifest"]["promotion_attestation_names"] == [
        step["gate"]["attestation"] for step in steps
    ]
    assert result["committed"]["ref"] == "dev"
    assert result["committed"]["identity_equal"]


async def test_command_defaults_to_the_bound_policy_and_the_designated_repository():
    drifted = trust_manifest.canonical_text(_committed(repository_id=1))
    handler = _handler(_Client(committed=drifted), bound_policy=_policy())

    result = await handler._cmd_integration_trust_manifest({"project_id": "agent-queue"})

    assert result["outcome"] == "manifest"
    assert result["policy_source"] == "bound" and result["repository_id"] == "agent-queue2"
    assert result["committed"]["code"] == "trust_manifest_mismatch"
    assert result["committed"]["diff"][0]["field"] == "repository_id"


async def test_command_reports_an_absent_committed_copy():
    handler = _handler(_Client(committed=None), bound_policy=_policy())

    result = await handler._cmd_integration_trust_manifest({"project_id": "agent-queue"})

    assert result["outcome"] == "manifest"
    assert result["committed"]["present"] is False
    assert result["committed"]["code"] == "trust_manifest_unavailable"
    assert result["committed"]["sha"] == SHA


async def test_command_is_refused_under_existing_login_credentials():
    client = _Client(identity=GitHubCredentialIdentity.existing_login())
    handler = _handler(client, bound_policy=_policy())

    result = await handler._cmd_integration_trust_manifest({"project_id": "agent-queue"})

    assert result["success"] is False and result["outcome"] == "not_app_mode"
    assert client.paths == []


async def test_command_refuses_a_slug_producer():
    handler = _handler(_Client(), bound_policy=_with_producers("github-actions", "github-actions"))

    result = await handler._cmd_integration_trust_manifest({"project_id": "agent-queue"})

    assert result["outcome"] == "ci_producer_not_numeric" and "error" in result


@pytest.mark.parametrize(
    ("args", "bound", "designated", "outcome"),
    [
        ({}, None, "agent-queue2", "policy_missing"),
        ({"policy": {"version": 1}}, None, "agent-queue2", "policy_invalid"),
        ({}, "policy", None, "repository_not_designated"),
        ({"repository_id": "elsewhere"}, "policy", "agent-queue2", "repository_mismatch"),
    ],
)
async def test_command_names_each_refusal(args, bound, designated, outcome):
    handler = _handler(
        _Client(), bound_policy=_policy() if bound else None, designated=designated
    )

    result = await handler._cmd_integration_trust_manifest({"project_id": "agent-queue", **args})

    assert result["outcome"] == outcome and result["success"] is False


async def test_command_is_refused_for_worker_tokens():
    worker = RequestScope(kind="session", session_id="w", task_id="t", project_id="agent-queue")
    refusal = check_command_scope(
        "integration_trust_manifest", {"project_id": "agent-queue"}, worker
    )
    assert refusal == "out of scope: integration_trust_manifest"

    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="w", project_id="agent-queue",
    )
    client = _Client()
    handler = _handler(client, bound_policy=_policy())
    with principal_context(principal):
        result = await handler._cmd_integration_trust_manifest({"project_id": "agent-queue"})
    assert result["outcome"] == "unauthorized"
    assert client.paths == []


def test_the_shipped_supervisor_profile_grants_the_command():
    profile = (REPO / "src/profiles/defaults/supervisor/profile.md").read_text()

    assert '"integration_trust_manifest"' in profile


async def test_contract_is_a_typed_read():
    from src.commands.contracts.builtin import set_handler_provider
    from src.commands.contracts.integration import register_integration_contracts

    registry = ContractRegistry()
    register_integration_contracts(registry)
    registration = registry.require("integration_trust_manifest")
    assert registration.contract.execution.side_effect is SideEffectClass.READ

    class StubHandler:
        async def execute(self, command, payload):
            assert command == "integration_trust_manifest"
            assert payload == {"project_id": "p", "policy": None, "repository_id": "r"}
            return {"outcome": "manifest", "text": "{}\n", "committed": {"present": False}}

    args = registration.contract.execution.args_model(project_id="p", repository_id="r")
    set_handler_provider(StubHandler)
    try:
        result = await registration.invoke(args, None)
    finally:
        set_handler_provider(None)
    assert result.outcome == "manifest"
    assert result.value.text == "{}\n" and result.value.committed == {"present": False}


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def _cli_result(committed_text: str | None = AGENT_QUEUE_MANIFEST) -> dict:
    manifest = json.loads(AGENT_QUEUE_MANIFEST)
    comparison = trust_manifest.compare(manifest, committed_text).as_dict()
    return {
        "outcome": "manifest",
        "project_id": "agent-queue",
        "repository_id": "agent-queue2",
        "policy_source": "argument",
        "github_repository_id": 1160639300,
        "full_name": "ElectricJack/agent-queue",
        "attestation_app_id": 5075923,
        "path": trust_manifest.TRUST_MANIFEST_PATH,
        "manifest": manifest,
        "text": AGENT_QUEUE_MANIFEST,
        "sha256": trust_manifest.text_sha256(AGENT_QUEUE_MANIFEST),
        "committed": {
            "path": trust_manifest.TRUST_MANIFEST_PATH, "ref": "main", "sha": SHA, **comparison,
        },
    }


def _invoke(argv, result):
    from src.cli.app import cli

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.execute = AsyncMock(return_value=result)
    with patch("src.cli.integration._get_client", return_value=client):
        outcome = CliRunner().invoke(cli, ["integration", "trust-manifest", *argv])
    return outcome, client


def test_cli_write_writes_the_canonical_text_from_the_policy_file(tmp_path):
    target = tmp_path / "agent-queue-integration.json"

    outcome, client = _invoke(
        [
            "agent-queue", "--policy", str(POLICY_PATH), "--repository-id", "agent-queue2",
            "--write", str(target),
        ],
        _cli_result(),
    )

    assert outcome.exit_code == 0, outcome.output
    assert target.read_text() == AGENT_QUEUE_MANIFEST
    client.execute.assert_awaited_once_with(
        "integration_trust_manifest",
        {"project_id": "agent-queue", "policy": _policy(), "repository_id": "agent-queue2"},
    )


def test_cli_print_emits_exactly_the_canonical_text():
    outcome, _client = _invoke(["agent-queue", "--print"], _cli_result())

    assert outcome.exit_code == 0, outcome.output
    assert outcome.output == AGENT_QUEUE_MANIFEST


def test_cli_check_exits_zero_on_an_equal_committed_copy():
    outcome, _client = _invoke(["agent-queue", "--check"], _cli_result())

    assert outcome.exit_code == 0, outcome.output
    assert "committed copy matches" in outcome.output


def test_cli_check_exits_one_with_a_field_diff_on_identity_drift():
    drifted = trust_manifest.canonical_text(_committed(attestation_app_id=5052310))

    outcome, _client = _invoke(
        ["agent-queue", "--policy", str(POLICY_PATH), "--check"], _cli_result(drifted)
    )

    assert outcome.exit_code == 1, outcome.output
    assert "trust_manifest_mismatch" in outcome.output
    assert "attestation_app_id (identity): expected 5075923, committed 5052310" in outcome.output
    assert (
        f"aq integration trust-manifest agent-queue --policy {POLICY_PATH} "
        "--write .github/agent-queue-integration.json"
    ) in outcome.output


def test_cli_check_exits_one_when_the_committed_copy_is_absent():
    outcome, _client = _invoke(["agent-queue", "--check"], _cli_result(None))

    assert outcome.exit_code == 1, outcome.output
    assert "committed copy unavailable" in outcome.output


def test_cli_check_reports_check_set_only_drift_as_a_warning():
    drifted = trust_manifest.canonical_text(_committed(required_checks__version="tests-yml-v2"))

    outcome, _client = _invoke(["agent-queue", "--check"], _cli_result(drifted))

    assert outcome.exit_code == 0, outcome.output
    assert "warning: trust_manifest_check_set_differs" in outcome.output
    assert "required_checks.version (check_set)" in outcome.output


def test_cli_check_json_keeps_the_envelope_and_the_exit_code():
    from src.cli.app import cli

    drifted = trust_manifest.canonical_text(_committed(repository_id=1))
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.execute = AsyncMock(return_value=_cli_result(drifted))
    with patch("src.cli.integration._get_client", return_value=client):
        outcome = CliRunner().invoke(
            cli, ["--json", "integration", "trust-manifest", "agent-queue", "--check"]
        )

    assert outcome.exit_code == 1
    data = json.loads(outcome.output)["data"]
    assert data["committed"]["code"] == "trust_manifest_mismatch"
    assert data["write_command"].endswith("--write .github/agent-queue-integration.json")


@pytest.mark.parametrize(
    "argv", [["agent-queue"], ["agent-queue", "--check", "--print"]]
)
def test_cli_needs_exactly_one_mode(argv):
    outcome, client = _invoke(argv, _cli_result())

    assert outcome.exit_code == 2
    assert "exactly one of --write PATH, --check and --print" in outcome.output
    client.execute.assert_not_awaited()
