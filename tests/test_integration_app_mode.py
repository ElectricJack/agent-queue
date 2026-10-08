"""App-mode readiness items: app-verify, app-setup and the preflight (spec §6.2-6.4)."""

from __future__ import annotations

import base64
import copy
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
from src.integration import app_mode, trust_manifest
from src.integration.models import HierarchicalIntegrationPolicy
from src.integration.preflight import daemon_functional_preflight
from src.profiles.capabilities import DENY_ALL

REPO = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO / "docs/config/agent-queue-train-policy.json"
SHA = "a" * 40
APP_ID = 5075923
RULESET_ID = 24002443
BINDING = GitHubRepositoryBinding(1160639300, "ElectricJack/agent-queue")
APP = GitHubCredentialIdentity.app(APP_ID, 164874645)
AUDIT_TEXT = (
    "name: Main attestation\n"
    "env:\n  APP_ID: ${{ vars.AQ_INTEGRATION_ATTESTATION_APP_ID }}\n"
)
#: The reviewed policy's root check-set version: what
#: ``AQ_INTEGRATION_REQUIRED_CHECK_VERSION`` must hold.
CHECK_VERSION = json.loads(POLICY_PATH.read_text())["root"]["required_checks"]["version"]


def _policy(**producers: str) -> dict:
    policy = json.loads(POLICY_PATH.read_text())
    for boundary, producer in producers.items():
        policy[boundary]["required_checks"]["producer_id"] = producer
    return policy


def _manifest() -> dict:
    return trust_manifest.manifest_for_policy(
        _policy(),
        canonical_repository_id="agent-queue2",
        repository_id=BINDING.repository_id,
        full_name=BINDING.full_name,
        attestation_app_id=APP_ID,
    )


def _encoded(text: str) -> dict:
    return {"encoding": "base64", "content": base64.encodebytes(text.encode()).decode()}


class FakeGitHub:
    """One repository as the daemon's App client sees it; every anchor in place."""

    repository = BINDING

    def __init__(self, identity: GitHubCredentialIdentity = APP) -> None:
        self.credential_identity = identity
        self.token_error: Exception | None = None
        self.remote: dict | None = {
            "id": BINDING.repository_id,
            "full_name": BINDING.full_name,
            "default_branch": "main",
        }
        self.files = {
            trust_manifest.TRUST_MANIFEST_PATH: trust_manifest.canonical_text(_manifest()),
            app_mode.AUDIT_WORKFLOW_PATH: AUDIT_TEXT,
            ".github/workflows/tests.yml": "name: Tests\n",
        }
        self.variables = {
            app_mode.APP_ID_VARIABLE: str(APP_ID),
            app_mode.CHECK_VERSION_VARIABLE: CHECK_VERSION,
        }
        # The §8.1 ruleset app-verify renders, applied: GitHub's effective rules.
        self.rules = [
            {
                **rule,
                "ruleset_source_type": "Repository",
                "ruleset_source": BINDING.full_name,
                "ruleset_id": RULESET_ID,
            }
            for rule in app_mode.target_ruleset(APP_ID, "main")["rules"]
        ]
        self.rulesets = {
            RULESET_ID: {
                "id": RULESET_ID, "name": "Train-only main", "current_user_can_bypass": "never",
            },
        }
        self.classic: dict | None = None
        self.paths: list[str] = []

    async def installation_token(self):
        if self.token_error is not None:
            raise self.token_error
        return "installation-token"

    async def exact_head_ref(self, branch):
        assert branch == "main"
        return SHA

    async def paged_list(self, path, *, max_pages):
        self.paths.append(path)
        if "/rules/branches/" in path:
            assert path == f"/repositories/{BINDING.repository_id}/rules/branches/main?per_page=100"
            return copy.deepcopy(self.rules)
        assert max_pages == 1
        assert path == f"/repositories/{BINDING.repository_id}/contents/.github/workflows?ref={SHA}"
        return [
            {"type": "file", "path": name}
            for name in self.files
            if name.startswith(".github/workflows/")
        ] + [{"type": "dir", "path": ".github/workflows/scripts"}]

    async def request_json(self, method, path):
        assert method == "GET"
        self.paths.append(path)
        if path == f"/repos/{BINDING.full_name}":
            if self.remote is None:
                raise GitHubAccessError("transient", "GitHub request failed (transient)")
            return self.remote
        if "/actions/variables/" in path:
            name = path.rsplit("/", 1)[-1]
            if name not in self.variables:
                raise GitHubAccessError("not_found_or_hidden", "GitHub request failed")
            return {"name": name, "value": self.variables[name]}
        root = f"/repositories/{BINDING.repository_id}"
        if path.startswith(f"{root}/rulesets/"):
            ruleset = self.rulesets.get(int(path.rsplit("/", 1)[1]))
            if ruleset is None:
                raise GitHubAccessError("not_found_or_hidden", "GitHub request failed")
            return copy.deepcopy(ruleset)
        if path == f"{root}/branches/main/protection":
            if self.classic is None:
                raise GitHubAccessError("not_found_or_hidden", "GitHub request failed")
            return copy.deepcopy(self.classic)
        prefix = f"/repositories/{BINDING.repository_id}/contents/"
        assert path.startswith(prefix) and path.endswith(f"?ref={SHA}"), path
        name = path[len(prefix):].split("?", 1)[0]
        if name not in self.files:
            raise GitHubAccessError("not_found_or_hidden", "GitHub request failed")
        return _encoded(self.files[name])


@pytest.fixture
def protected(monkeypatch):
    """Default-branch protection reads as ``attested_only`` without the reader's reads."""

    async def attested_only(_ctx):
        return app_mode.AppModeItem(
            "protection", app_mode.OK, observed={"classification": "attested_only"}
        )

    monkeypatch.setattr(app_mode, "check_protection", attested_only)


def _project(policy: dict, mode: str = "observe") -> SimpleNamespace:
    return SimpleNamespace(
        id="agent-queue",
        hierarchical_integration_policy=policy,
        hierarchical_integration_mode=mode,
        integration_repository_id="agent-queue2",
    )


def _db(policy: dict) -> SimpleNamespace:
    repository = SimpleNamespace(
        id="agent-queue2",
        project_id="agent-queue",
        default_branch="main",
        url="https://github.com/ElectricJack/agent-queue.git",
    )
    # No list_profiles: preflight gates on the class hints only; the router
    # assigns repair and verifier profiles.
    return SimpleNamespace(
        get_project=AsyncMock(return_value=_project(policy)),
        get_repo=AsyncMock(side_effect=lambda rid: repository if rid == "agent-queue2" else None),
        get_session=AsyncMock(return_value=None),
    )


def _orchestrator(client: FakeGitHub, policy: dict) -> SimpleNamespace:
    """Every runtime dependency wired and the routes' artifacts loaded."""
    parsed = HierarchicalIntegrationPolicy.model_validate(_policy())
    definitions = {}
    classes = set()
    for boundary in (parsed.parent, parsed.root):
        route, artifact = boundary.route, boundary.route.artifact
        definitions[artifact.artifact_sha256] = SimpleNamespace(
            id=route.playbook_id,
            scope=SimpleNamespace(type=route.scope, project_id=route.scope_identifier),
            schema_version=artifact.schema_generation,
            source_hash=artifact.source_digest,
            version=artifact.version,
            contract_fingerprint=lambda value=artifact.contract_fingerprint: value,
        )
        classes |= {
            boundary.primary_intelligence_class,
            boundary.repair.debug_intelligence_class,
            boundary.verifier_intelligence_class,
        }
    return SimpleNamespace(
        db=_db(policy),
        github_client_factory=lambda _binding: client,
        github_repository_binding_resolver=AsyncMock(return_value=BINDING),
        playbook_manager=SimpleNamespace(_store=SimpleNamespace(load=definitions.__getitem__)),
        integration_attestation_service=object(),
        root_promotion_service=object(),
        integration_cleanup_service=object(),
        git=object(),
        intelligence_classes={name: object() for name in classes},
    )


def _handler(client: FakeGitHub, policy: dict | None = None) -> IntegrationCommandsMixin:
    orchestrator = _orchestrator(client, policy or _policy())
    handler = IntegrationCommandsMixin()
    handler.db = orchestrator.db
    handler.orchestrator = orchestrator
    return handler


# ---------------------------------------------------------------------------
# One report, two readers: the preflight and app-verify agree on every item
# ---------------------------------------------------------------------------


def _set(target: dict, key: str, value) -> None:
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


def _committed(**changes) -> str:
    manifest = _manifest()
    for dotted, value in changes.items():
        target = manifest
        *parents, leaf = dotted.split("__")
        for part in parents:
            target = target[part]
        target[leaf] = value
    return trust_manifest.canonical_text(manifest)


SCENARIOS = {
    # item id, how GitHub (or the policy) differs, blockers, warnings
    "all ok": (lambda gh: None, {}, (), ()),
    "credential: the token does not mint": (
        lambda gh: setattr(gh, "token_error", GitHubAccessError("permission", "no grant")),
        {},
        ("app_token_unavailable",),
        (),
    ),
    "repository: another id": (
        lambda gh: gh.remote.update(id=1), {}, ("repository_mismatch",), (),
    ),
    "repository: another default branch": (
        lambda gh: gh.remote.update(default_branch="trunk"), {}, ("repository_mismatch",), (),
    ),
    "repository: unreadable": (
        lambda gh: setattr(gh, "remote", None), {}, ("repository_mismatch",), (),
    ),
    "producer: a slug": (
        lambda gh: None,
        {"parent": "github-actions", "root": "github-actions"},
        ("ci_producer_not_numeric",),
        (),
    ),
    "producer: two numeric producers": (
        lambda gh: None, {"root": "4242"}, ("ci_producer_mismatch",), (),
    ),
    "manifest: absent": (
        lambda gh: gh.files.pop(trust_manifest.TRUST_MANIFEST_PATH),
        {},
        ("trust_manifest_unavailable",),
        (),
    ),
    "manifest: another attestation App": (
        lambda gh: gh.files.update(
            {trust_manifest.TRUST_MANIFEST_PATH: _committed(attestation_app_id=5052310)}
        ),
        {},
        ("trust_manifest_mismatch",),
        (),
    ),
    "manifest: the check set lags the policy": (
        lambda gh: gh.files.update(
            {trust_manifest.TRUST_MANIFEST_PATH: _committed(required_checks__version="v2")}
        ),
        {},
        (),
        ("trust_manifest_check_set_differs",),
    ),
    "manifest: not canonical": (
        lambda gh: gh.files.update(
            {trust_manifest.TRUST_MANIFEST_PATH: json.dumps(_manifest(), indent=4)}
        ),
        {},
        (),
        ("trust_manifest_noncanonical",),
    ),
    "variables: one unset": (
        lambda gh: _set(gh.variables, app_mode.CHECK_VERSION_VARIABLE, None),
        {},
        ("hosted_workflow_variables_unavailable",),
        (),
    ),
    "variables: a stale version": (
        lambda gh: _set(gh.variables, app_mode.CHECK_VERSION_VARIABLE, "tests-yml-v2"),
        {},
        ("hosted_workflow_variables_mismatch",),
        (),
    ),
    "variables: another App id": (
        lambda gh: _set(gh.variables, app_mode.APP_ID_VARIABLE, "5052310"),
        {},
        ("hosted_workflow_variables_mismatch",),
        (),
    ),
    "audit_workflow: absent": (
        lambda gh: gh.files.pop(app_mode.AUDIT_WORKFLOW_PATH),
        {},
        (),
        ("audit_workflow_missing",),
    ),
    "audit_workflow: under another name": (
        lambda gh: gh.files.update(
            {".github/workflows/audit.yaml": gh.files.pop(app_mode.AUDIT_WORKFLOW_PATH)}
        ),
        {},
        (),
        (),
    ),
    "several at once": (
        lambda gh: (
            gh.files.pop(app_mode.AUDIT_WORKFLOW_PATH),
            _set(gh.variables, app_mode.APP_ID_VARIABLE, None),
            gh.remote.update(id=1),
        ),
        {"root": "4242"},
        (
            "repository_mismatch",
            "ci_producer_mismatch",
            "hosted_workflow_variables_unavailable",
        ),
        ("audit_workflow_missing",),
    ),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_preflight_and_app_verify_produce_identical_codes(protected, scenario):
    mutate, producers, blockers, warnings = SCENARIOS[scenario]
    client = FakeGitHub()
    mutate(client)
    policy = _policy(**producers)

    preflight = await daemon_functional_preflight(
        _orchestrator(client, policy), "agent-queue", "agent-queue2"
    )
    verified = await _handler(client, policy)._cmd_integration_app_verify(
        {"project_id": "agent-queue"}
    )

    assert verified["outcome"] == "verified"
    assert (tuple(preflight), preflight.warnings) == (blockers, warnings)
    assert (tuple(verified["blockers"]), tuple(verified["warnings"])) == (blockers, warnings)
    assert verified["ready"] is (not blockers)
    assert [item["id"] for item in verified["items"]] == list(app_mode.ITEM_IDS)
    failing = {
        code for item in verified["items"] if item["status"] == "fail" for code in item["codes"]
    }
    assert failing == set(blockers)


def _bypass(gh: FakeGitHub, value: str | None) -> None:
    _set(gh.rulesets[RULESET_ID], "current_user_can_bypass", value)


PROTECTION = {
    # how GitHub's protection differs from §8.1, blockers, warnings, classification
    "the §8.1 ruleset": (lambda gh: None, (), (), "attested_only"),
    "the App bypass kept": (
        lambda gh: _bypass(gh, "always"), (), ("main_protection_app_bypass",), "app_bypass",
    ),
    "a pull request rule": (
        lambda gh: gh.rules.append(
            {**gh.rules[0], "type": "pull_request", "parameters": {}}
        ),
        ("branch_protection_incompatible",),
        (),
        "incompatible",
    ),
    "no ruleset": (
        lambda gh: (gh.rules.clear(), gh.rulesets.clear()),
        ("main_protection_missing",),
        (),
        "unprotected",
    ),
    "the bypass unreported": (
        lambda gh: _bypass(gh, None), ("main_protection_unverifiable",), (), "unverifiable",
    ),
    "classic protection pinning the attestation": (
        lambda gh: (
            gh.rules.clear(),
            gh.rulesets.clear(),
            setattr(gh, "classic", {"required_status_checks": {
                "strict": False,
                "contexts": [trust_manifest.ATTESTATION_NAME],
                "checks": [{"context": trust_manifest.ATTESTATION_NAME, "app_id": APP_ID}],
            }}),
        ),
        (),
        (),
        "attested_only",
    ),
}


@pytest.mark.parametrize("scenario", list(PROTECTION))
async def test_protection_is_the_same_codes_in_the_preflight_and_app_verify(scenario):
    mutate, blockers, warnings, classification = PROTECTION[scenario]
    client = FakeGitHub()
    mutate(client)

    preflight = await daemon_functional_preflight(
        _orchestrator(client, _policy()), "agent-queue", "agent-queue2"
    )
    verified = await _handler(client)._cmd_integration_app_verify({"project_id": "agent-queue"})

    assert (tuple(preflight), preflight.warnings) == (blockers, warnings)
    assert (tuple(verified["blockers"]), tuple(verified["warnings"])) == (blockers, warnings)
    protection = verified["items"][app_mode.ITEM_IDS.index("protection")]
    assert protection["observed"]["classification"] == classification
    assert protection["expected"] == {"classification": "attested_only"}
    assert protection["codes"] == list(blockers or warnings)
    assert (protection["fix"] is None) is (not blockers and not warnings)


@pytest.mark.parametrize(
    "bypass, status, codes",
    [
        ("never", "fail", ["main_protection_blocks_development_publisher"]),
        ("always", "ok", []),
    ],
)
async def test_a_development_project_needs_the_app_bypass(bypass, status, codes):
    client = FakeGitHub()
    _bypass(client, bypass)
    handler = _handler(client)
    handler.db.get_project.return_value = _project(_policy(), mode="development")

    result = await handler._cmd_integration_app_verify({"project_id": "agent-queue"})

    protection = result["items"][app_mode.ITEM_IDS.index("protection")]
    assert (protection["status"], protection["codes"]) == (status, codes)
    assert protection["expected"] == {"classification": "app_bypass"}
    if codes:
        assert f'{{"actor_id": {APP_ID}, "actor_type": "Integration"' in protection["fix"]


async def test_a_disabled_project_is_judged_like_the_preflight():
    """Runbook §9.3 steps 3-5 run app-verify while disabled; status judges it strictly too."""
    client = FakeGitHub()
    _bypass(client, None)
    handler = _handler(client)
    handler.db.get_project.return_value = _project(_policy(), mode="disabled")

    result = await handler._cmd_integration_app_verify({"project_id": "agent-queue"})
    preflight = await daemon_functional_preflight(
        _orchestrator(client, _policy()), "agent-queue", "agent-queue2"
    )

    protection = result["items"][app_mode.ITEM_IDS.index("protection")]
    assert protection["status"] == "fail"
    assert protection["observed"]["classification"] == "unverifiable"
    assert protection["expected"] == {"classification": "attested_only"}
    assert tuple(result["blockers"]) == tuple(preflight) == ("main_protection_unverifiable",)


# ---------------------------------------------------------------------------
# The items
# ---------------------------------------------------------------------------


async def test_every_item_is_ok_with_the_anchors_in_place(protected):
    client = FakeGitHub()

    result = await _handler(client)._cmd_integration_app_verify({"project_id": "agent-queue"})

    assert result["ready"] is True and result["blockers"] == [] and result["warnings"] == []
    assert {item["id"]: item["status"] for item in result["items"]} == {
        item_id: "ok" for item_id in app_mode.ITEM_IDS
    }
    items = {item["id"]: item for item in result["items"]}
    assert items["credential"]["observed"] == {
        "token": True, "mode": "app", "app_id": APP_ID, "installation_id": 164874645,
    }
    assert items["producer"]["expected"] == "15368"
    assert items["audit_workflow"]["observed"]["path"] == app_mode.AUDIT_WORKFLOW_PATH
    # Every read went through the App client at the default branch's exact SHA.
    assert all(f"?ref={SHA}" in path for path in client.paths if "/contents/" in path)


@pytest.mark.parametrize("drift", [None, "missing", "another-attestation"])
async def test_verify_and_preflight_compare_bound_promotion_identities(protected, monkeypatch, drift):
    steps = [
        {"id": "staging", "source": "main", "target": "staging",
         "gate": {"attestation": "Agent Queue Promotion Attestation (staging)"}},
        {"id": "release", "source": "staging", "target": "production",
         "gate": {"attestation": "Agent Queue Promotion Attestation (release)"}},
    ]
    names = [step["gate"]["attestation"] for step in steps]
    stored = AsyncMock(return_value=steps)
    monkeypatch.setattr("src.integration.promotion_steps.read_stored_promotion_flow", stored)
    client = FakeGitHub()
    manifest = _manifest()
    manifest["promotion_attestation_names"] = names.copy()
    if drift == "missing":
        manifest.pop("promotion_attestation_names")
    elif drift == "another-attestation":
        manifest["promotion_attestation_names"][1] = "Unconfigured release attestation"
    client.files[trust_manifest.TRUST_MANIFEST_PATH] = trust_manifest.canonical_text(manifest)
    result = await _handler(client)._cmd_integration_app_verify({"project_id": "agent-queue"})
    item = next(item for item in result["items"] if item["id"] == "manifest")
    preflight = await daemon_functional_preflight(
        _orchestrator(client, _policy()), "agent-queue", "agent-queue2",
    )
    assert stored.await_count == 2
    assert result["expected"]["manifest"]["promotion_attestation_names"] == names
    if drift is None:
        assert item["status"] == "ok"
        assert "trust_manifest_mismatch" not in result["blockers"]
        assert "trust_manifest_mismatch" not in preflight
    else:
        assert item["status"] == "fail"
        assert [change["field"] for change in item["observed"]["diff"]] == [
            "promotion_attestation_names",
        ]
        assert "trust_manifest_mismatch" in result["blockers"]
        assert "trust_manifest_mismatch" in preflight


async def test_expected_carries_the_manifest_the_variables_and_the_ruleset(protected):
    result = await _handler(FakeGitHub())._cmd_integration_app_verify(
        {"project_id": "agent-queue"}
    )

    expected = result["expected"]
    assert expected["manifest"] == _manifest()
    assert expected["manifest_text"] == trust_manifest.canonical_text(_manifest())
    assert expected["variables"] == {
        "AQ_INTEGRATION_ATTESTATION_APP_ID": "5075923",
        "AQ_INTEGRATION_REQUIRED_CHECK_VERSION": CHECK_VERSION,
    }
    # Spec §8.1, pinned to this App, with no bypass.
    assert expected["ruleset"] == {
        "name": "Train-only main",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [
                        {"context": "Agent Queue Integration Attestation", "integration_id": APP_ID}
                    ],
                },
            }
        ],
        "bypass_actors": [],
    }


async def test_variables_answer_to_the_policy_not_the_manifest(protected):
    client = FakeGitHub()
    # A rotation in flight: the manifest still names the old version.
    client.files[trust_manifest.TRUST_MANIFEST_PATH] = _committed(
        required_checks__version="tests-yml-v2"
    )

    result = await _handler(client)._cmd_integration_app_verify({"project_id": "agent-queue"})

    items = {item["id"]: item for item in result["items"]}
    assert items["variables"]["status"] == "ok"
    assert items["manifest"]["status"] == "warn"
    assert items["manifest"]["codes"] == ["trust_manifest_check_set_differs"]


RECORDED_VARIABLES = REPO / "tests/fixtures/app_mode"


class RecordedVariables(FakeGitHub):
    """Serves both Actions variables exactly as GitHub returned them to the fixture.

    Recorded 2026-09-28 after the live proof's rotation switch (spec §10 S8):
    ``GET /repos/ElectricJack/aq-gh615-app-fixture-20260923/actions/variables/<name>``.
    """

    async def request_json(self, method, path):
        if "/actions/variables/" not in path:
            return await super().request_json(method, path)
        self.paths.append(path)
        name = path.rsplit("/", 1)[-1]
        return json.loads((RECORDED_VARIABLES / f"fixture-variable-{name}.json").read_text())


async def test_recorded_variables_answer_to_the_bound_policy_version(protected):
    """GitHub's payload carries timestamps beside the name and value; the item reads the
    value and compares it with the policy alone, so the same recording is ok for the
    policy the switch bound and a mismatch for one it did not."""
    switched = _policy()
    switched["root"]["required_checks"]["version"] = "fixture-v2"

    ok = await _handler(RecordedVariables(), switched)._cmd_integration_app_verify(
        {"project_id": "agent-queue"}
    )
    stale = await _handler(RecordedVariables())._cmd_integration_app_verify(
        {"project_id": "agent-queue"}
    )

    variables = {item["id"]: item for item in ok["items"]}["variables"]
    assert variables["status"] == "ok"
    assert variables["observed"]["values"] == {
        app_mode.APP_ID_VARIABLE: str(APP_ID),
        app_mode.CHECK_VERSION_VARIABLE: "fixture-v2",
    }
    assert {item["id"]: item for item in stale["items"]}["variables"]["codes"] == [
        "hosted_workflow_variables_mismatch"
    ]


async def test_a_slug_producer_leaves_the_manifest_compared_on_every_other_field(protected):
    client = FakeGitHub()
    client.files[trust_manifest.TRUST_MANIFEST_PATH] = _committed(full_name="ElectricJack/other")
    policy = _policy(parent="github-actions", root="github-actions")

    result = await _handler(client, policy)._cmd_integration_app_verify(
        {"project_id": "agent-queue"}
    )

    items = {item["id"]: item for item in result["items"]}
    assert items["producer"]["code"] == "ci_producer_not_numeric"
    assert items["manifest"]["code"] == "trust_manifest_mismatch"
    assert [entry["field"] for entry in items["manifest"]["observed"]["diff"]] == ["full_name"]
    # No producer, so no manifest text to write until the policy is fixed.
    assert result["expected"]["manifest_text"] is None


async def test_a_failed_credential_is_named_once_and_nothing_else_is_read(protected):
    client = FakeGitHub()
    client.token_error = GitHubAccessError("credentials", "private key is unreadable")

    result = await _handler(client)._cmd_integration_app_verify({"project_id": "agent-queue"})

    items = {item["id"]: item for item in result["items"]}
    assert items["credential"]["code"] == "app_token_unavailable"
    assert "credentials: private key is unreadable" in items["credential"]["observed"]["error"]
    for item_id in ("repository", "manifest", "variables", "protection"):
        assert items[item_id]["status"] == "fail"
        assert items[item_id]["codes"] == ["app_token_unavailable"]
        assert items[item_id]["observed"]["checked"] is False
    assert items["audit_workflow"]["status"] == "warn"
    assert items["producer"]["status"] == "ok"
    assert result["blockers"] == ["app_token_unavailable"] and result["warnings"] == []
    assert client.paths == []


async def test_existing_login_credentials_are_reported_not_refused():
    client = FakeGitHub(identity=GitHubCredentialIdentity.existing_login())

    result = await _handler(client)._cmd_integration_app_verify({"project_id": "agent-queue"})

    assert result["success"] is True and result["outcome"] == "verified"
    assert result["blockers"] == ["not_app_mode"] and result["ready"] is False
    credential = result["items"][0]
    assert credential["code"] == "not_app_mode"
    assert "integration.github_app" in credential["fix"]
    assert result["expected"]["ruleset"] is None
    assert client.paths == []


async def test_fixes_repeat_the_commands_own_inputs(protected):
    client = FakeGitHub()
    client.variables.clear()
    client.files.pop(trust_manifest.TRUST_MANIFEST_PATH)

    result = await _handler(client)._cmd_integration_app_verify(
        {
            "project_id": "agent-queue",
            "policy": _policy(),
            "policy_path": "docs/config/agent-queue-train-policy.json",
            "repository_id": "agent-queue2",
        }
    )

    items = {item["id"]: item for item in result["items"]}
    inputs = (
        "agent-queue --policy docs/config/agent-queue-train-policy.json "
        "--repository-id agent-queue2"
    )
    assert items["variables"]["fix"] == (
        f"aq integration app-setup {inputs} --apply (runbook §9.3 step 3)"
    )
    assert items["manifest"]["fix"].startswith(
        f"aq integration trust-manifest {inputs} --write .github/agent-queue-integration.json"
    )
    assert items["variables"]["observed"]["unavailable"] == {
        "AQ_INTEGRATION_ATTESTATION_APP_ID": "absent",
        "AQ_INTEGRATION_REQUIRED_CHECK_VERSION": "absent",
    }
    assert result["policy_source"] == "argument"


@pytest.mark.parametrize(
    ("args", "bound", "outcome"),
    [
        ({}, None, "policy_missing"),
        ({"policy": {"version": 1}}, None, "policy_invalid"),
        ({"repository_id": "elsewhere"}, "policy", "repository_mismatch"),
    ],
)
async def test_the_command_refuses_only_for_its_inputs(args, bound, outcome):
    handler = _handler(FakeGitHub())
    handler.db.get_project = AsyncMock(return_value=_project(_policy() if bound else None))

    result = await handler._cmd_integration_app_verify({"project_id": "agent-queue", **args})

    assert result["outcome"] == outcome and result["success"] is False


async def test_the_command_is_refused_for_worker_tokens():
    worker = RequestScope(kind="session", session_id="w", task_id="t", project_id="agent-queue")
    assert check_command_scope(
        "integration_app_verify", {"project_id": "agent-queue"}, worker
    ) == "out of scope: integration_app_verify"

    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION, policy=DENY_ALL, session_id="w", project_id="agent-queue",
    )
    client = FakeGitHub()
    with principal_context(principal):
        result = await _handler(client)._cmd_integration_app_verify({"project_id": "agent-queue"})
    assert result["outcome"] == "unauthorized"
    assert client.paths == []


def test_the_shipped_supervisor_profile_grants_the_command():
    profile = (REPO / "src/profiles/defaults/supervisor/profile.md").read_text()

    assert '"integration_app_verify"' in profile


async def test_contract_is_a_typed_read():
    from src.commands.contracts.builtin import set_handler_provider
    from src.commands.contracts.integration import register_integration_contracts

    registry = ContractRegistry()
    register_integration_contracts(registry)
    registration = registry.require("integration_app_verify")
    assert registration.contract.execution.side_effect is SideEffectClass.READ

    report = await _handler(FakeGitHub())._cmd_integration_app_verify(
        {"project_id": "agent-queue"}
    )

    class StubHandler:
        async def execute(self, command, payload):
            assert command == "integration_app_verify"
            assert payload == {
                "project_id": "p", "policy": None, "repository_id": None, "policy_path": None,
            }
            return report

    args = registration.contract.execution.args_model(project_id="p")
    set_handler_provider(StubHandler)
    try:
        result = await registration.invoke(args, None)
    finally:
        set_handler_provider(None)
    assert result.outcome == "verified"
    assert [item["id"] for item in result.value.items] == list(app_mode.ITEM_IDS)
    assert result.value.expected["ruleset"]["rules"][0]["type"] == "required_status_checks"


async def test_status_contract_carries_warnings():
    from src.commands.contracts.builtin import set_handler_provider
    from src.commands.contracts.integration import register_integration_contracts

    registry = ContractRegistry()
    register_integration_contracts(registry)
    registration = registry.require("integration_status")
    warning = {"code": "audit_workflow_missing", "detail": "d", "ref": "agent-queue2"}
    activity = {"scope": "daemon", "api_calls_per_minute": {"GET repos/acme/widgets": 3}}

    class StubHandler:
        async def execute(self, command, payload):
            return {"outcome": "status", "ready": True, "blockers": [], "warnings": [warning],
                    "github": activity}

    set_handler_provider(StubHandler)
    try:
        result = await registration.invoke(
            registration.contract.execution.args_model(project_id="p"), None
        )
    finally:
        set_handler_provider(None)
    assert result.value.ready is True and result.value.warnings == (warning,)
    assert result.value.github == activity


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def _cli(
    argv, client: FakeGitHub, *, policy: dict | None = None, global_args=(), mode: str = "observe"
):
    """Run ``aq integration ...`` against the real handler over ``client``."""
    from src.cli.app import cli

    handler = _handler(client, policy)
    handler.db.get_project.return_value = _project(policy or _policy(), mode=mode)
    calls: list[tuple[str, dict]] = []

    async def execute(command, args):
        calls.append((command, args))
        assert command == "integration_app_verify"
        return await handler._cmd_integration_app_verify(args)

    api = AsyncMock()
    api.__aenter__ = AsyncMock(return_value=api)
    api.__aexit__ = AsyncMock(return_value=False)
    api.execute = AsyncMock(side_effect=execute)
    with patch("src.cli.integration._get_client", return_value=api):
        outcome = CliRunner().invoke(cli, [*global_args, "integration", *argv])
    return outcome, calls


def test_cli_app_verify_renders_every_item_and_exits_one_when_not_ready():
    client = FakeGitHub()
    client.variables.pop(app_mode.CHECK_VERSION_VARIABLE)
    client.rules.clear()

    outcome, calls = _cli(["app-verify", "agent-queue"], client)

    assert outcome.exit_code == 1, outcome.output
    assert calls == [("integration_app_verify", {"project_id": "agent-queue"})]
    for item_id in app_mode.ITEM_IDS:
        assert item_id in outcome.output
    assert "hosted_workflow_variables_unavailable" in outcome.output
    assert "main_protection_missing" in outcome.output
    assert "aq integration app-setup agent-queue --apply" in outcome.output
    assert "not ready: 2 blocker(s), 0 warning(s)" in outcome.output


def test_cli_app_verify_passes_the_policy_file_and_exits_zero_when_ready(protected):
    outcome, calls = _cli(
        ["app-verify", "agent-queue", "--policy", str(POLICY_PATH), "--repository-id",
         "agent-queue2"],
        FakeGitHub(),
    )

    assert outcome.exit_code == 0, outcome.output
    assert calls[0][1] == {
        "project_id": "agent-queue",
        "policy": _policy(),
        "policy_path": str(POLICY_PATH),
        "repository_id": "agent-queue2",
    }
    assert "ready: 0 blocker(s), 0 warning(s)" in outcome.output


def _gh(client: FakeGitHub, runs: list[list[str]]):
    """A fake ``subprocess.run`` whose ``gh variable set`` writes the fake repository."""

    def run(argv, **kwargs):
        runs.append(argv)
        assert argv[:3] == ["gh", "variable", "set"] and kwargs["timeout"] == 60
        name, repo, value = argv[3], argv[5], argv[7]
        assert argv[4] == "--repo" and argv[6] == "--body" and repo == BINDING.full_name
        client.variables[name] = value
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_app_setup_apply_sets_only_the_differing_variables_and_reverifies(protected):
    client = FakeGitHub()
    client.variables[app_mode.CHECK_VERSION_VARIABLE] = "tests-yml-v2"
    runs: list[list[str]] = []

    with patch("subprocess.run", side_effect=_gh(client, runs)):
        outcome, calls = _cli(["app-setup", "agent-queue", "--apply"], client)

    assert outcome.exit_code == 0, outcome.output
    # The App id was already right, so gh never touched it.
    assert runs == [[
        "gh", "variable", "set", "AQ_INTEGRATION_REQUIRED_CHECK_VERSION",
        "--repo", "ElectricJack/agent-queue", "--body", CHECK_VERSION,
    ]]
    # Verified, applied, then verified again through the App.
    assert [command for command, _args in calls] == ["integration_app_verify"] * 2
    assert "AQ_INTEGRATION_REQUIRED_CHECK_VERSION: set" in outcome.output
    reverified = outcome.output.split("re-verified:", 1)[1]
    assert "ok   variables" in reverified


def test_app_setup_apply_sets_both_when_both_are_unset(protected):
    client = FakeGitHub()
    client.variables.clear()
    runs: list[list[str]] = []

    with patch("subprocess.run", side_effect=_gh(client, runs)):
        outcome, _calls = _cli(["app-setup", "agent-queue", "--apply"], client)

    assert outcome.exit_code == 0, outcome.output
    assert [argv[3] for argv in runs] == list(app_mode.HOSTED_VARIABLES)
    assert [argv[7] for argv in runs] == ["5075923", CHECK_VERSION]


def test_app_setup_apply_touches_nothing_when_the_variables_are_correct(protected):
    runs: list[list[str]] = []
    client = FakeGitHub()

    with patch("subprocess.run", side_effect=_gh(client, runs)):
        outcome, calls = _cli(["app-setup", "agent-queue", "--apply"], client)

    assert outcome.exit_code == 0, outcome.output
    assert runs == [] and len(calls) == 1
    assert "both variables are already correct" in outcome.output


def test_app_setup_apply_exits_one_when_gh_fails(protected):
    client = FakeGitHub()
    client.variables.clear()

    def refused(argv, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 403: Must have admin rights")

    with patch("subprocess.run", side_effect=refused):
        outcome, _calls = _cli(["app-setup", "agent-queue", "--apply"], client)

    assert outcome.exit_code == 1, outcome.output
    assert "failed: HTTP 403: Must have admin rights" in outcome.output


def test_app_setup_without_apply_prints_commands_manifest_and_ruleset_steps():
    client = FakeGitHub()
    client.variables.clear()
    client.files.pop(trust_manifest.TRUST_MANIFEST_PATH)
    client.rules.clear()

    with patch("subprocess.run", side_effect=AssertionError("gh must not run")):
        outcome, calls = _cli(
            ["app-setup", "agent-queue", "--policy", str(POLICY_PATH)], client
        )

    assert outcome.exit_code == 0, outcome.output
    assert len(calls) == 1
    output = " ".join(outcome.output.split())
    assert (
        "gh variable set AQ_INTEGRATION_ATTESTATION_APP_ID --repo ElectricJack/agent-queue "
        "--body 5075923"
    ) in output
    assert (
        "gh variable set AQ_INTEGRATION_REQUIRED_CHECK_VERSION --repo "
        f"ElectricJack/agent-queue --body {CHECK_VERSION}"
    ) in output
    assert (
        f"aq integration trust-manifest agent-queue --policy {POLICY_PATH} "
        "--write .github/agent-queue-integration.json"
    ) in output
    assert "then commit .github/agent-queue-integration.json to main of " in output
    # The ruleset is printed, never applied.
    assert '"integration_id": 5075923' in outcome.output
    assert "gh api --method PUT repos/ElectricJack/agent-queue/rulesets/RULESET_ID" in output
    assert "gh api --method POST repos/ElectricJack/agent-queue/rulesets --input FILE" in output


def test_app_setup_names_the_train_only_ruleset():
    """Spec §9.3 step 4: the ruleset named like the target is the one to update."""
    client = FakeGitHub()
    _bypass(client, "always")

    outcome, _calls = _cli(["app-setup", "agent-queue"], client)

    output = " ".join(outcome.output.split())
    assert "main_protection_app_bypass (app_bypass)" in output
    assert f"gh api --method PUT repos/ElectricJack/agent-queue/rulesets/{RULESET_ID}" in output
    assert "--method POST" not in output


def test_app_setup_never_targets_a_ruleset_named_otherwise():
    """PUT replaces a whole ruleset: one with another name may hold other rules."""
    client = FakeGitHub()
    _bypass(client, "always")
    client.rulesets[RULESET_ID]["name"] = "main protection"

    outcome, _calls = _cli(["app-setup", "agent-queue"], client)

    output = " ".join(outcome.output.split())
    assert "gh api --method PUT repos/ElectricJack/agent-queue/rulesets/RULESET_ID" in output
    assert "gh api --method POST repos/ElectricJack/agent-queue/rulesets --input FILE" in output


@pytest.mark.parametrize(
    "mode, mutate, code",
    [
        ("development", lambda gh: None, "main_protection_blocks_development_publisher"),
        ("observe", lambda gh: _bypass(gh, None), "main_protection_unverifiable"),
    ],
)
def test_app_setup_prints_the_item_fix_where_the_target_ruleset_would_not_help(
    mode, mutate, code
):
    client = FakeGitHub()
    mutate(client)

    outcome, _calls = _cli(["app-setup", "agent-queue"], client, mode=mode)

    output = " ".join(outcome.output.split())
    assert code in output
    assert "gh api --method" not in output and '"bypass_actors": []' not in outcome.output
    fix = "bypass actors" if mode == "development" else "administration: read"
    assert fix in output


def test_app_setup_json_keeps_the_envelope(protected):
    client = FakeGitHub()
    client.variables[app_mode.APP_ID_VARIABLE] = "1"

    outcome, _calls = _cli(["app-setup", "agent-queue"], client, global_args=["--json"])

    assert outcome.exit_code == 0, outcome.output
    data = json.loads(outcome.output)["data"]
    assert data["variables"]["differing"] == ["AQ_INTEGRATION_ATTESTATION_APP_ID"]
    assert data["variables"]["applied"] is None
    assert data["protection"]["ruleset"]["bypass_actors"] == []
