"""The default branch's protection, classified for the App (App-mode spec §8.2-8.3)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.commands.integration_commands import IntegrationCommandsMixin
from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubRepositoryBinding,
)
from src.integration import protection
from src.integration.attestation import IntegrationAttestationService
from src.integration.models import HierarchicalIntegrationPolicy
from src.integration.protection import (
    APP_BYPASS,
    ATTESTED_ONLY,
    INCOMPATIBLE,
    UNPROTECTED,
    UNVERIFIABLE,
    classify,
)
from src.integration.trust_manifest import ATTESTATION_NAME

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests/fixtures/github_protection"
POLICY_PATH = REPO / "docs/config/agent-queue-train-policy.json"
POLICY = HierarchicalIntegrationPolicy.model_validate(json.loads(POLICY_PATH.read_text()))
APP_ID = 5075923
PRODUCER = 15368
BINDING = GitHubRepositoryBinding(1160639300, "ElectricJack/agent-queue")
APP = GitHubCredentialIdentity.app(APP_ID, 164874645)
BASIC, TRAIN = 21960669, 24002443


def _recorded(name: str):
    return json.loads((FIXTURES / name).read_text())


def _rule(rule_type: str, ruleset_id: int, parameters=None, *, source=("Repository", None)):
    source_type, owner = source
    rule = {
        "type": rule_type,
        "ruleset_source_type": source_type,
        "ruleset_source": owner or BINDING.full_name,
        "ruleset_id": ruleset_id,
    }
    if parameters is not None:
        rule["parameters"] = parameters
    return rule


def _checks(ruleset_id: int, *checks: dict, source=("Repository", None)):
    return _rule(
        "required_status_checks",
        ruleset_id,
        {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": False,
            "required_status_checks": list(checks),
        },
        source=source,
    )


ATTESTATION = {"context": ATTESTATION_NAME, "integration_id": APP_ID}


def _target_rules():
    """The recorded rules after §8.1: "Train-only main" requires the pinned attestation."""
    recorded = _recorded("agent-queue-rules-main.json")
    return [*(rule for rule in recorded if rule["ruleset_id"] == BASIC), _checks(TRAIN, ATTESTATION)]


def _classic(**changes):
    payload = _recorded("fixture-classic-protection.json")
    payload.update(changes)
    return payload


# ---------------------------------------------------------------------------
# The five classes from recorded payloads
# ---------------------------------------------------------------------------


def test_recorded_agent_queue_protection_today_is_app_bypass():
    """Spec §2: "Train-only main" requires an unpinned status, bypassed only by the App."""
    rules = _recorded("agent-queue-rules-main.json")
    assert {rule["ruleset_id"] for rule in rules} == {BASIC, TRAIN}

    reading = classify(rules, {BASIC: "never", TRAIN: "always"}, None, app_id=APP_ID, policy=POLICY)

    assert reading.classification == APP_BYPASS
    assert reading.development_publisher_blocked is False
    assert [(rule.type, rule.bypass) for rule in reading.rules] == [
        ("deletion", "never"),
        ("non_fast_forward", "never"),
        ("required_status_checks", "always"),
    ]


def test_the_section_8_1_ruleset_is_attested_only():
    reading = classify(
        _target_rules(), {BASIC: "never", TRAIN: "never"}, None, app_id=APP_ID, policy=POLICY
    )

    assert reading.classification == ATTESTED_ONLY
    assert reading.development_publisher_blocked is True
    assert [rule.requires_attestation for rule in reading.rules] == [False, False, True]


@pytest.mark.parametrize("bypass", ["always", "exempt"])
def test_the_section_8_1_ruleset_with_the_app_bypass_is_app_bypass(bypass):
    """Spec §10 S2: temporarily adding the App bypass reads as app_bypass."""
    reading = classify(
        _target_rules(), {BASIC: "never", TRAIN: bypass}, None, app_id=APP_ID, policy=POLICY
    )

    assert reading.classification == APP_BYPASS
    assert reading.development_publisher_blocked is False


def test_a_pull_request_only_bypass_does_not_let_a_push_through():
    reading = classify(
        _target_rules(),
        {BASIC: "never", TRAIN: "pull_requests_only"},
        None,
        app_id=APP_ID,
        policy=POLICY,
    )

    assert reading.classification == ATTESTED_ONLY


def test_the_recorded_unpinned_status_without_the_bypass_is_incompatible():
    """``aq-train/candidate`` is unpinned: satisfiable by anyone, so it is no protection."""
    reading = classify(
        _recorded("agent-queue-rules-main.json"),
        {BASIC: "never", TRAIN: "never"},
        None,
        app_id=APP_ID,
        policy=POLICY,
    )

    assert reading.classification == INCOMPATIBLE
    assert f"ruleset {TRAIN} required_status_checks" in reading.reason
    assert "aq-train/candidate" in reading.reason


@pytest.mark.parametrize(
    "rules, classic",
    [
        ([], None),
        ([_rule("deletion", BASIC), _rule("non_fast_forward", BASIC), _rule("creation", BASIC)],
         None),
        ([], _classic(required_status_checks=None)),
    ],
    ids=["nothing", "basic protection only", "classic protection without checks"],
)
def test_no_pinned_attestation_is_unprotected(rules, classic):
    reading = classify(rules, {BASIC: "never"}, classic, app_id=APP_ID, policy=POLICY)

    assert reading.classification == UNPROTECTED
    assert reading.development_publisher_blocked is False


def test_the_fixture_classic_protection_is_unprotected_and_blocks_the_publisher():
    """Spec §2: classic protection requires ``fixture`` from App 15368, no attestation."""
    raw = json.loads(POLICY_PATH.read_text())
    raw["root"]["required_checks"] = {
        "version": "fixture-v1", "names": ["fixture"], "producer_id": "15368",
    }
    fixture_policy = HierarchicalIntegrationPolicy.model_validate(raw)

    reading = classify([], {}, _classic(), app_id=APP_ID, policy=fixture_policy)

    assert reading.classification == UNPROTECTED
    # A development publisher's commit never carries ``fixture``.
    assert reading.development_publisher_blocked is True
    assert [rule.as_dict() for rule in reading.rules] == [
        {"source": "classic", "type": "required_status_checks", "bypass": "never",
         "refuses_unattested": True}
    ]
    # Without the policy the check is neither the attestation nor a policy check.
    assert classify([], {}, _classic(), app_id=APP_ID).classification == INCOMPATIBLE


def test_classic_protection_pinning_the_attestation_is_attested_only():
    status = {"strict": True, "contexts": [ATTESTATION_NAME],
              "checks": [{"context": ATTESTATION_NAME, "app_id": APP_ID}]}

    reading = classify(
        [], {}, _classic(required_status_checks=status), app_id=APP_ID, policy=POLICY
    )

    assert reading.classification == ATTESTED_ONLY


def test_policy_checks_pinned_to_the_producer_pass_an_attested_fast_forward():
    name = POLICY.root.required_checks.names[0]
    pinned = classify(
        [_checks(TRAIN, ATTESTATION, {"context": name, "integration_id": PRODUCER})],
        {TRAIN: "never"},
        None,
        app_id=APP_ID,
        policy=POLICY,
    )
    unpinned = classify(
        [_checks(TRAIN, ATTESTATION, {"context": name})],
        {TRAIN: "never"},
        None,
        app_id=APP_ID,
        policy=POLICY,
    )

    assert pinned.classification == ATTESTED_ONLY
    assert unpinned.classification == INCOMPATIBLE
    assert name in unpinned.reason


@pytest.mark.parametrize(
    "check",
    [
        {"context": ATTESTATION_NAME},
        {"context": ATTESTATION_NAME, "integration_id": 5052310},
        {"context": ATTESTATION_NAME, "integration_id": str(APP_ID)},
    ],
    ids=["unpinned", "another App", "not an integer"],
)
def test_an_attestation_not_pinned_to_the_app_counts_as_none(check):
    enforced = classify([_checks(TRAIN, check)], {TRAIN: "never"}, None, app_id=APP_ID)
    bypassed = classify([_checks(TRAIN, check)], {TRAIN: "always"}, None, app_id=APP_ID)

    assert enforced.classification == INCOMPATIBLE
    assert bypassed.classification == APP_BYPASS


@pytest.mark.parametrize(
    "rule_type",
    [
        "pull_request",
        "required_signatures",
        "required_deployments",
        "update",
        "merge_queue",
        "required_linear_history",
        "file_path_restriction",
        "max_file_size",
        "commit_message_pattern",
        "a_rule_type_github_adds_later",
    ],
)
def test_rules_that_refuse_an_attested_fast_forward(rule_type):
    rules = [*_target_rules(), _rule(rule_type, 777, {})]

    enforced = classify(rules, {BASIC: "never", TRAIN: "never", 777: "never"}, None,
                        app_id=APP_ID, policy=POLICY)
    bypassed = classify(rules, {BASIC: "never", TRAIN: "never", 777: "always"}, None,
                        app_id=APP_ID, policy=POLICY)

    assert enforced.classification == INCOMPATIBLE
    assert enforced.reason.startswith(f"ruleset 777 {rule_type}: ")
    # The App gets past that rule; the attestation still binds it.
    assert bypassed.classification == ATTESTED_ONLY


def test_organization_rulesets_are_judged_like_repository_ones():
    org = ("Organization", "ElectricJack")
    pull_request = _rule("pull_request", 900001, {"required_approving_review_count": 1},
                         source=org)
    attestation = _checks(900002, ATTESTATION, source=org)

    blocked = classify([*_target_rules(), pull_request],
                       {BASIC: "never", TRAIN: "never", 900001: "never"}, None,
                       app_id=APP_ID, policy=POLICY)
    org_only = classify([attestation], {900002: "never"}, None, app_id=APP_ID, policy=POLICY)
    org_bypass = classify([attestation], {900002: "always"}, None, app_id=APP_ID, policy=POLICY)

    assert blocked.classification == INCOMPATIBLE
    assert blocked.reason.startswith("organization ruleset 900001 pull_request: ")
    assert blocked.rules[-1].as_dict()["ruleset_source_type"] == "Organization"
    assert org_only.classification == ATTESTED_ONLY
    assert org_bypass.classification == APP_BYPASS


@pytest.mark.parametrize(
    "changes, rule_type",
    [
        ({"required_pull_request_reviews": {"required_approving_review_count": 1}},
         "pull_request"),
        ({"required_signatures": {"enabled": True}}, "required_signatures"),
        ({"required_linear_history": {"enabled": True}}, "required_linear_history"),
        ({"lock_branch": {"enabled": True}}, "lock_branch"),
        ({"restrictions": {"users": [], "teams": [], "apps": [{"id": 1, "slug": "other"}]}},
         "restrictions"),
        ({"required_status_checks": {"strict": False, "contexts": ["fixture"]}},
         "required_status_checks"),
    ],
)
def test_classic_protection_that_refuses_the_app_is_incompatible(changes, rule_type):
    reading = classify([], {}, _classic(**changes), app_id=APP_ID, policy=POLICY)

    assert reading.classification == INCOMPATIBLE
    assert f"classic protection {rule_type}: " in reading.reason


def test_classic_allowances_that_list_the_app_let_it_through():
    app = {"id": APP_ID, "slug": "agent-queue-train"}
    classic = _classic(
        required_status_checks={"strict": False, "contexts": [],
                                "checks": [{"context": ATTESTATION_NAME, "app_id": APP_ID}]},
        required_pull_request_reviews={
            "bypass_pull_request_allowances": {"users": [], "teams": [], "apps": [app]},
        },
        restrictions={"users": [], "teams": [], "apps": [app]},
    )

    assert classify([], {}, classic, app_id=APP_ID, policy=POLICY).classification == ATTESTED_ONLY


def test_classic_and_ruleset_protection_are_layered():
    reading = classify(
        _target_rules(),
        {BASIC: "never", TRAIN: "always"},
        _classic(required_status_checks={
            "strict": False, "contexts": [],
            "checks": [{"context": ATTESTATION_NAME, "app_id": APP_ID}],
        }),
        app_id=APP_ID,
        policy=POLICY,
    )

    # The ruleset is bypassed, but classic protection pins the attestation.
    assert reading.classification == ATTESTED_ONLY


# ---------------------------------------------------------------------------
# Reads through the App: a failure is unverifiable, never unprotected
# ---------------------------------------------------------------------------


class FakeProtectionClient:
    """The App client's view of one repository's protection."""

    repository = BINDING

    def __init__(self, rules=None, rulesets=None, classic=None, identity=APP):
        self.credential_identity = identity
        self.rules = _target_rules() if rules is None else rules
        self.rulesets = rulesets if rulesets is not None else {
            BASIC: {**_recorded(f"agent-queue-ruleset-{BASIC}.json"),
                    "current_user_can_bypass": "never"},
            TRAIN: {**_recorded(f"agent-queue-ruleset-{TRAIN}.json"),
                    "current_user_can_bypass": "never"},
        }
        self.classic = classic
        self.errors: dict[str, Exception] = {}
        self.requests: list[tuple[str, str]] = []

    def _fail(self, key: str) -> None:
        if key in self.errors:
            raise self.errors[key]

    async def paged_list(self, path, *, max_pages):
        self.requests.append(("GET", path))
        assert path == f"/repositories/{BINDING.repository_id}/rules/branches/main?per_page=100"
        assert max_pages == protection.MAX_RULE_PAGES
        self._fail("rules")
        return copy.deepcopy(self.rules)

    async def request_json(self, method, path):
        self.requests.append((method, path))
        root = f"/repositories/{BINDING.repository_id}"
        if path.startswith(f"{root}/rulesets/"):
            ruleset_id = int(path.rsplit("/", 1)[1])
            self._fail(f"ruleset {ruleset_id}")
            if ruleset_id not in self.rulesets:
                raise GitHubAccessError("not_found_or_hidden", "GitHub request failed")
            return copy.deepcopy(self.rulesets[ruleset_id])
        assert path == f"{root}/branches/main/protection", path
        self._fail("classic")
        if self.classic is None:
            raise GitHubAccessError("not_found_or_hidden", "GitHub request failed")
        return copy.deepcopy(self.classic)


async def _read(client: FakeProtectionClient, branch: str | None = "main"):
    return await protection.read_protection(client, BINDING, branch, app_id=APP_ID, policy=POLICY)


async def test_read_protection_reads_three_things_and_writes_nothing():
    client = FakeProtectionClient()

    reading = await _read(client)

    assert reading.classification == ATTESTED_ONLY
    assert [rule.ruleset_name for rule in reading.rules] == [
        "Basic Protection", "Basic Protection", "Train-only main",
    ]
    root = f"/repositories/{BINDING.repository_id}"
    assert client.requests == [
        ("GET", f"{root}/rules/branches/main?per_page=100"),
        ("GET", f"{root}/rulesets/{BASIC}"),
        ("GET", f"{root}/rulesets/{TRAIN}"),
        ("GET", f"{root}/branches/main/protection"),
    ]


async def test_a_classic_protection_404_is_none():
    client = FakeProtectionClient(rules=[], rulesets={})

    assert (await _read(client)).classification == UNPROTECTED


def _without_bypass(client):
    client.rulesets[TRAIN] = _recorded(f"agent-queue-ruleset-{TRAIN}.json")


def _unknown_bypass(client):
    client.rulesets[TRAIN]["current_user_can_bypass"] = "sometimes"


UNREADABLE = {
    "rules: permission": lambda c: c.errors.update(
        rules=GitHubAccessError("permission", "GitHub request failed")),
    "rules: not found": lambda c: c.errors.update(
        rules=GitHubAccessError("not_found_or_hidden", "GitHub request failed")),
    "rules: malformed": lambda c: setattr(c, "rules", [{"type": "deletion"}]),
    "ruleset: transient": lambda c: c.errors.update(
        {f"ruleset {TRAIN}": GitHubAccessError("transient", "GitHub request failed")}),
    "ruleset: not found": lambda c: c.rulesets.pop(TRAIN),
    "ruleset: another id": lambda c: c.rulesets[TRAIN].update(id=1),
    # The recorded anonymous read: GitHub omits the field (spec §10 S2 open question).
    "ruleset: no current_user_can_bypass": _without_bypass,
    "ruleset: an unknown bypass value": _unknown_bypass,
    "classic: permission": lambda c: c.errors.update(
        classic=GitHubAccessError("permission", "GitHub request failed")),
    "classic: malformed": lambda c: setattr(c, "classic", {"required_status_checks": []}),
    "client: raises": lambda c: c.errors.update(rules=RuntimeError("boom")),
}


@pytest.mark.parametrize("scenario", list(UNREADABLE))
async def test_a_failed_read_is_unverifiable_never_unprotected(scenario):
    # With nothing enforced, a guess would say "unprotected"; the reader never guesses.
    client = FakeProtectionClient()
    UNREADABLE[scenario](client)

    reading = await _read(client)

    assert reading.classification == UNVERIFIABLE
    assert reading.rules == ()
    assert reading.reason
    assert reading.development_publisher_blocked is None
    assert protection.judge(reading, None) == (("main_protection_unverifiable",), ())
    assert protection.judge(reading, "development") == (("main_protection_unverifiable",), ())
    assert all(method == "GET" for method, _path in client.requests)


async def test_no_default_branch_is_unverifiable():
    client = FakeProtectionClient()

    reading = await _read(client, None)

    assert (reading.classification, client.requests) == (UNVERIFIABLE, [])


# ---------------------------------------------------------------------------
# Mode coupling (§8.2)
# ---------------------------------------------------------------------------


def _reading(classification, *, blocked=False):
    rule = protection.ProtectionRule(
        source="ruleset", type="required_status_checks", bypass="never",
        refuses_unattested=blocked,
    )
    return protection.ProtectionReading(classification, (rule,))


@pytest.mark.parametrize(
    "classification, blocked, mode, expected",
    [
        (ATTESTED_ONLY, True, None, ((), ())),
        (ATTESTED_ONLY, True, "train", ((), ())),
        (APP_BYPASS, False, "hierarchy", ((), ("main_protection_app_bypass",))),
        (APP_BYPASS, False, "observe", ((), ("main_protection_app_bypass",))),
        (INCOMPATIBLE, True, "train", (("branch_protection_incompatible",), ())),
        (UNPROTECTED, False, "train", (("main_protection_missing",), ())),
        (UNVERIFIABLE, False, "train", (("main_protection_unverifiable",), ())),
        (APP_BYPASS, False, "development", ((), ())),
        (UNPROTECTED, False, "development", ((), ())),
        (UNPROTECTED, True, "development",
         (("main_protection_blocks_development_publisher",), ())),
        (ATTESTED_ONLY, True, "development",
         (("main_protection_blocks_development_publisher",), ())),
        (INCOMPATIBLE, True, "development",
         (("main_protection_blocks_development_publisher", "branch_protection_incompatible"),
          ())),
        (UNVERIFIABLE, False, "disabled", ((), ())),
        (UNPROTECTED, False, "disabled", ((), ())),
        (INCOMPATIBLE, True, "disabled", ((), ())),
    ],
)
def test_judge_couples_the_classification_to_the_mode(classification, blocked, mode, expected):
    reading = (
        protection.ProtectionReading(UNVERIFIABLE, reason="x")
        if classification == UNVERIFIABLE
        else _reading(classification, blocked=blocked)
    )

    assert protection.judge(reading, mode) == expected


@pytest.mark.parametrize(
    "mode, required",
    [(None, ATTESTED_ONLY), ("observe", ATTESTED_ONLY), ("hierarchy", ATTESTED_ONLY),
     ("train", ATTESTED_ONLY), ("development", APP_BYPASS), ("disabled", None)],
)
def test_required_classification_per_mode(mode, required):
    assert protection.required_classification(mode) == required


# ---------------------------------------------------------------------------
# The attestation service's protection_reader seam
# ---------------------------------------------------------------------------


def _repository(url="https://github.com/ElectricJack/agent-queue.git"):
    return SimpleNamespace(
        id="agent-queue2", project_id="agent-queue", default_branch="main", url=url,
    )


def _db():
    return SimpleNamespace(
        get_repo=AsyncMock(
            side_effect=lambda rid: _repository() if rid == "agent-queue2" else None
        ),
        get_project=AsyncMock(
            return_value=SimpleNamespace(
                hierarchical_integration_policy=json.loads(POLICY.model_dump_json())
            )
        ),
    )


def _reader(client):
    return protection.enablement_reader(
        _db(),
        binding_resolver=AsyncMock(return_value=BINDING),
        client_factory=lambda _binding: client,
    )


async def test_the_enablement_reader_is_true_under_attested_only():
    assert await _reader(FakeProtectionClient())("agent-queue2") is True


async def test_the_enablement_reader_names_each_strict_blocker():
    unprotected = FakeProtectionClient(rules=[], rulesets={})
    incompatible = FakeProtectionClient(rules=_recorded("agent-queue-rules-main.json"))
    unverifiable = FakeProtectionClient()
    _without_bypass(unverifiable)

    assert await _reader(unprotected)("agent-queue2") == ("main_protection_missing",)
    assert await _reader(incompatible)("agent-queue2") == ("branch_protection_incompatible",)
    assert await _reader(unverifiable)("agent-queue2") == ("main_protection_unverifiable",)
    assert await _reader(unverifiable)("elsewhere") == ("main_protection_unverifiable",)


async def test_the_enablement_reader_reports_a_database_failure_as_unverifiable():
    db = _db()
    db.get_repo.side_effect = OSError("database unavailable")
    reader = protection.enablement_reader(
        db,
        binding_resolver=AsyncMock(return_value=BINDING),
        client_factory=lambda _binding: FakeProtectionClient(),
    )

    assert await reader("agent-queue2") == ("main_protection_unverifiable",)


async def test_the_enablement_reader_leaves_existing_login_alone():
    client = FakeProtectionClient(rules=[], identity=GitHubCredentialIdentity.existing_login())

    assert await _reader(client)("agent-queue2") is True
    assert client.requests == []


async def test_the_attestation_service_reports_the_reader_blockers():
    client = FakeProtectionClient(rules=[], rulesets={})
    service = IntegrationAttestationService(
        _db(),
        data_dir="/nonexistent",
        git_manager=object(),
        github_client_factory=lambda _binding: client,
        protection_reader=_reader(client),
        probe_reader=lambda _rid: True,
        debug_class_reader=lambda _rid: True,
    )

    result = await service.enablement_blockers("agent-queue2")

    assert (result.ready, result.blockers) == (False, ("main_protection_missing",))


# ---------------------------------------------------------------------------
# The development guard
# ---------------------------------------------------------------------------


async def _guard(client, repository=None, resolver=None):
    return await protection.development_guard(
        repository or _repository(),
        binding_resolver=resolver or AsyncMock(return_value=BINDING),
        client_factory=lambda _binding: client,
    )


async def test_the_development_guard_refuses_under_attested_only():
    with pytest.raises(protection.DevelopmentPublisherBlocked) as refused:
        await _guard(FakeProtectionClient())

    assert refused.value.reading.classification == ATTESTED_ONLY
    assert refused.value.blocker()["code"] == "main_protection_blocks_development_publisher"
    assert refused.value.blocker()["ref"] == "agent-queue2"
    message = str(refused.value)
    assert message.startswith("main_protection_blocks_development_publisher: ")
    assert f'"actor_id": {APP_ID}, "actor_type": "Integration"' in message
    # Only the ruleset that refuses the App is named; Basic Protection does not.
    assert f"bypass actors of ruleset {TRAIN} required_status_checks" in message
    assert str(BASIC) not in message and "classic" not in message


async def test_a_classic_refusal_names_classic_protection_not_a_bypass():
    client = FakeProtectionClient(rules=[], rulesets={}, classic=_classic())

    with pytest.raises(protection.DevelopmentPublisherBlocked) as refused:
        await _guard(client)

    message = str(refused.value)
    assert "remove classic protection's required_status_checks" in message
    assert "actor_id" not in message


async def test_the_development_guard_refuses_under_incompatible():
    client = FakeProtectionClient(rules=[_rule("pull_request", 777, {})],
                                  rulesets={777: {"id": 777, "current_user_can_bypass": "never"}})

    with pytest.raises(protection.DevelopmentPublisherBlocked) as refused:
        await _guard(client)

    assert refused.value.reading.classification == INCOMPATIBLE
    assert "ruleset 777 pull_request" in str(refused.value)


async def test_the_development_guard_allows_app_bypass():
    client = FakeProtectionClient()
    client.rulesets[TRAIN]["current_user_can_bypass"] = "always"

    reading = await _guard(client)

    assert reading.classification == APP_BYPASS


async def test_the_development_guard_allows_an_unprotected_branch():
    reading = await _guard(FakeProtectionClient(rules=[], rulesets={}))

    assert reading.classification == UNPROTECTED


async def test_the_development_guard_reports_unverifiable_without_refusing():
    client = FakeProtectionClient()
    _without_bypass(client)

    reading = await _guard(client)

    assert reading.classification == UNVERIFIABLE


async def test_the_development_guard_reports_a_client_failure_as_unverifiable():
    reading = await _guard(
        FakeProtectionClient(),
        resolver=AsyncMock(side_effect=GitHubAccessError("permission", "not installed")),
    )

    assert reading.classification == UNVERIFIABLE
    assert "permission" in reading.reason


async def test_the_development_guard_does_not_apply_without_github_app_credentials():
    local = await _guard(FakeProtectionClient(), resolver=AsyncMock(return_value=None))
    existing = FakeProtectionClient(identity=GitHubCredentialIdentity.existing_login())

    assert local is None
    assert await _guard(existing) is None
    assert existing.requests == []


async def test_existing_login_skips_the_guard_before_any_github_call():
    resolver = AsyncMock(side_effect=AssertionError("no binding under existing-login"))

    reading = await protection.development_guard(
        _repository(),
        binding_resolver=resolver,
        client_factory=lambda _binding: FakeProtectionClient(),
        identity=GitHubCredentialIdentity.existing_login(),
    )

    assert reading is None
    resolver.assert_not_called()


async def test_the_guard_names_policy_checks_with_the_bound_policy():
    """A producer-pinned policy check is not blamed as incompatible when the policy is known."""
    name = POLICY.root.required_checks.names[0]
    client = FakeProtectionClient(
        rules=[_checks(TRAIN, ATTESTATION, {"context": name, "integration_id": PRODUCER})],
        rulesets={TRAIN: {"id": TRAIN, "current_user_can_bypass": "never"}},
    )

    with pytest.raises(protection.DevelopmentPublisherBlocked) as with_policy:
        await protection.development_guard(
            _repository(),
            binding_resolver=AsyncMock(return_value=BINDING),
            client_factory=lambda _binding: client,
            policy=POLICY,
        )

    assert with_policy.value.reading.classification == ATTESTED_ONLY


class _Development:
    """``DevelopmentIntegration.configure`` as far as the guard is concerned."""

    def __init__(self):
        self.configured = []

    async def configure(self, project_id, policy, *, reason, operator_id, protection_guard):
        reading = await protection_guard(_repository()) if protection_guard else None
        self.configured.append(project_id)
        result = {"outcome": "configured", "project_id": project_id,
                  "repository_id": "agent-queue2", "policy": policy}
        if reading is not None:
            result["evidence"] = {"protection": reading.as_dict()}
        return result


def _develop_handler(client, development, identity=APP):
    handler = IntegrationCommandsMixin()
    handler.db = _db()
    handler.orchestrator = SimpleNamespace(
        github_repository_binding_resolver=AsyncMock(return_value=BINDING),
        github_client_factory=lambda _binding: client,
        github_access=SimpleNamespace(credential_identity=identity),
    )
    handler._development_integration = lambda: development
    return handler


async def _develop(handler):
    with patch(
        "src.commands.integration_commands.integration_operator",
        return_value=("human:local-operator", None),
    ):
        return await handler._cmd_integration_develop(
            {"project_id": "agent-queue", "policy": {"validation": "none"}, "reason": "rollback"}
        )


async def test_integration_develop_is_refused_under_attested_only():
    development = _Development()

    result = await _develop(_develop_handler(FakeProtectionClient(), development))

    assert result["success"] is False and result["outcome"] == "blocked"
    assert [blocker["code"] for blocker in result["blockers"]] == [
        "main_protection_blocks_development_publisher"
    ]
    assert result["error"].startswith("main_protection_blocks_development_publisher: ")
    assert development.configured == []


async def test_integration_develop_reads_nothing_under_existing_login():
    client = FakeProtectionClient()
    development = _Development()
    handler = _develop_handler(
        client, development, identity=GitHubCredentialIdentity.existing_login()
    )

    result = await _develop(handler)

    assert result["outcome"] == "configured" and "evidence" not in result
    handler.orchestrator.github_repository_binding_resolver.assert_not_called()
    assert client.requests == []


async def test_integration_develop_is_allowed_under_app_bypass():
    client = FakeProtectionClient()
    client.rulesets[TRAIN]["current_user_can_bypass"] = "always"
    development = _Development()

    result = await _develop(_develop_handler(client, development))

    assert result["outcome"] == "configured"
    assert result["evidence"]["protection"]["classification"] == APP_BYPASS
    assert development.configured == ["agent-queue"]
