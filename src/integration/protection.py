"""The default branch's protection, read and classified for the daemon's App.

App-mode integration train spec §8.2-8.3.  Three reads through the App's
installation token (``administration: read``, already granted), none of which
writes anything:

* the effective rules for the default branch (``rules/branches/{branch}``).
  They include parent and organization rulesets, and each rule names its
  ``ruleset_id``;
* per distinct ruleset, ``current_user_can_bypass`` for the App's own token.
  The App cannot read another actor's bypass list, and does not need to;
* classic branch protection, where a 404 means none.

:func:`classify` judges them for the App's promotion push, a fast-forward to
an attested candidate:

``unverifiable``
    a read failed or answered something unexpected.  Never "unprotected".
``incompatible``
    a rule the App cannot bypass would refuse an attested fast-forward.
``attested_only``
    the App cannot bypass, and the attestation pinned to its id is required.
``app_bypass``
    the App can bypass every rule that would refuse an unattested push.
``unprotected``
    no rule requires the attestation pinned to the App id.

:func:`judge` couples a classification to a project mode (§8.2).  Three
callers share it: the ``protection`` item of ``app-verify`` and the preflight
(:mod:`src.integration.app_mode`), the attestation service's enablement probe
(:func:`enablement_reader`), and the development guard
(:func:`development_guard`) that refuses a development publisher the App's
push could not get past.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError

from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubCredentialMode,
    GitHubRepositoryBinding,
    credential_identity_from_client,
)
from src.integration.ci import is_numeric_producer_id
from src.integration.models import HierarchicalIntegrationPolicy
from src.integration.trust_manifest import ATTESTATION_NAME

UNVERIFIABLE = "unverifiable"
APP_BYPASS = "app_bypass"
INCOMPATIBLE = "incompatible"
ATTESTED_ONLY = "attested_only"
UNPROTECTED = "unprotected"


class LocalRulesetReader:
    """Explicit operator login, limited to reading one bound ruleset's detail.

    The App's administration:read token hides bypass actors. A LOCAL command
    may supply this reader to observe those actors with the operator's gh login.
    The App remains the authority for all writes and its own bypass ability.
    """

    def __init__(self, client: Any):
        if credential_identity_from_client(client).mode is not GitHubCredentialMode.EXISTING_LOGIN:
            raise ValueError("ruleset admin reader requires the existing operator login")
        if not isinstance(client.repository, GitHubRepositoryBinding):
            raise ValueError("ruleset admin reader requires a repository binding")
        self.client = client

    async def detail(self, binding: GitHubRepositoryBinding, ruleset_id: int) -> dict:
        if binding != self.client.repository or type(ruleset_id) is not int or ruleset_id <= 0:
            raise ValueError("ruleset admin reader repository or ruleset does not match")
        document = await self.client.request_json(
            "GET", f"/repositories/{binding.repository_id}/rulesets/{ruleset_id}",
        )
        if (type(document.get("id")) is not int or document["id"] != ruleset_id
                or not isinstance(document.get("bypass_actors"), list)):
            raise ValueError("github_ruleset_unverifiable: operator bypass actors are hidden")
        return document


async def read_ruleset_detail(
    client: Any, binding: GitHubRepositoryBinding, ruleset_id: int, *,
    admin_reader: LocalRulesetReader | None = None,
) -> dict:
    """Supplement hidden actors only after both readers agree on the live policy."""
    if client.repository != binding or type(ruleset_id) is not int or ruleset_id <= 0:
        raise ValueError("ruleset reader repository or ruleset does not match")
    document = await client.request_json(
        "GET", f"/repositories/{binding.repository_id}/rulesets/{ruleset_id}",
    )
    if type(document.get("id")) is not int or document["id"] != ruleset_id:
        raise ValueError("github_ruleset_unverifiable: ruleset answered another id")
    if isinstance(document.get("bypass_actors"), list) or admin_reader is None:
        return document
    operator_document = await admin_reader.detail(binding, ruleset_id)
    fields = ("id", "name", "target", "enforcement", "conditions", "rules", *(
        field for field in ("source", "source_type")
        if field in document or field in operator_document
    ))
    if any(field not in document or field not in operator_document
           or document[field] != operator_document[field] for field in fields):
        raise ValueError("github_ruleset_unverifiable: App and operator policy observations differ")
    # In particular, current_user_can_bypass belongs to the App observation.
    return {**document, "bypass_actors": operator_document["bypass_actors"]}


CLASSIFICATIONS = (UNVERIFIABLE, APP_BYPASS, INCOMPATIBLE, ATTESTED_ONLY, UNPROTECTED)

MISSING_CODE = "main_protection_missing"
INCOMPATIBLE_CODE = "branch_protection_incompatible"
UNVERIFIABLE_CODE = "main_protection_unverifiable"
APP_BYPASS_CODE = "main_protection_app_bypass"
BLOCKS_DEVELOPMENT_CODE = "main_protection_blocks_development_publisher"

#: Every ``current_user_can_bypass`` value; the first two let the App's push through.
BYPASSING = frozenset({"always", "exempt"})
BYPASS_VALUES = BYPASSING | {"pull_requests_only", "never"}
#: Classic protection has no bypass for an App beyond the allowances it lists.
CLASSIC_BYPASS = "never"

#: Rule types that an attested fast-forward of an existing branch always satisfies.
SATISFIED_RULES = frozenset({"creation", "deletion", "non_fast_forward"})
#: Why each named rule type refuses an attested fast-forward (spec §8.3).
#: A type named nowhere refuses it too: the reader never guesses.
REFUSING_RULES = {
    "pull_request": "a promotion is a direct push, not a pull request",
    "required_signatures": "candidate commits are unsigned",
    "required_deployments": "a candidate is not deployed before it is promoted",
    "update": "the branch may not be updated",
    "lock_branch": "the branch is read-only",
    "merge_queue": "the branch accepts merges only through a merge queue",
    "required_linear_history": "candidates are merge commits",
    "file_path_restriction": "a file path restriction can refuse a candidate's changes",
    "file_extension_restriction": "a file extension restriction can refuse a candidate's changes",
    "max_file_path_length": "a file path length limit can refuse a candidate's changes",
    "max_file_size": "a file size limit can refuse a candidate's changes",
    "restrictions": "the App is not among the actors allowed to push",
}
#: Pages of effective rules read before the list counts as unreadable.
MAX_RULE_PAGES = 10


class ProtectionUnreadable(ValueError):
    """A protection read answered something the reader will not interpret."""


@dataclass(frozen=True)
class ProtectionRule:
    """One effective rule, judged for the App's push."""

    #: ``ruleset`` or ``classic``.
    source: str
    type: str
    #: The App's ``current_user_can_bypass`` for the rule's ruleset.
    bypass: str
    #: Why an attested fast-forward is refused; ``None`` when it passes.
    refusal: str | None = None
    #: The rule requires the attestation pinned to the App id.
    requires_attestation: bool = False
    #: The rule refuses a push without the attestation or CI (the development
    #: publisher's): any required check, and every rule that refuses an attested one.
    refuses_unattested: bool = False
    ruleset_id: int | None = None
    #: ``Repository`` or ``Organization``, as GitHub reports it.
    ruleset_source_type: str | None = None
    ruleset_source: str | None = None
    ruleset_name: str | None = None

    @property
    def bypassed(self) -> bool:
        return self.bypass in BYPASSING

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"source": self.source, "type": self.type, "bypass": self.bypass}
        if self.ruleset_id is not None:
            value.update(
                ruleset_id=self.ruleset_id,
                ruleset_name=self.ruleset_name,
                ruleset_source_type=self.ruleset_source_type,
                ruleset_source=self.ruleset_source,
            )
        if self.refusal is not None:
            value["refusal"] = self.refusal
        if self.requires_attestation:
            value["requires_attestation"] = True
        if self.refuses_unattested:
            value["refuses_unattested"] = True
        return value


@dataclass(frozen=True)
class ProtectionReading:
    classification: str
    rules: tuple[ProtectionRule, ...] = ()
    #: Unverifiable: the read that failed.  Incompatible: the refusing rules.
    reason: str | None = None

    @property
    def development_publisher_blocked(self) -> bool | None:
        """Whether a rule the App cannot bypass refuses the publisher's push.

        True under ``attested_only`` and ``incompatible``, and under
        ``unprotected`` when a required check (pinned to the CI producer) is
        enforced on the App.  ``None`` when the protection is unverifiable.
        """
        if self.classification == UNVERIFIABLE:
            return None
        return any(rule.refuses_unattested and not rule.bypassed for rule in self.rules)

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "classification": self.classification,
            "rules": [rule.as_dict() for rule in self.rules],
            "development_publisher_blocked": self.development_publisher_blocked,
        }
        if self.reason is not None:
            value["reason"] = self.reason
        return value


def required_classification(mode: str | None) -> str | None:
    """The classification a project mode needs (spec §8.2); ``None`` is any.

    ``None`` stands for the strict modes (``observe``, ``hierarchy``,
    ``train``), which the preflight gates.
    """
    if mode == "disabled":
        return None
    if mode == "development":
        return APP_BYPASS
    return ATTESTED_ONLY


def judge(reading: ProtectionReading, mode: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The blocker and warning codes of *reading* under a project mode (spec §8.2).

    ``disabled`` accepts any protection: nothing in AQ pushes the default
    branch.  ``development`` needs the publisher's unattested push to get
    through.  The strict modes need ``attested_only`` and accept
    ``app_bypass`` with a warning, for the transition only.
    """
    if mode == "disabled":
        return (), ()
    if reading.classification == UNVERIFIABLE:
        return (UNVERIFIABLE_CODE,), ()
    if mode == "development":
        if not reading.development_publisher_blocked:
            return (), ()
        if reading.classification == INCOMPATIBLE:
            return (BLOCKS_DEVELOPMENT_CODE, INCOMPATIBLE_CODE), ()
        return (BLOCKS_DEVELOPMENT_CODE,), ()
    if reading.classification == ATTESTED_ONLY:
        return (), ()
    if reading.classification == APP_BYPASS:
        return (), (APP_BYPASS_CODE,)
    if reading.classification == INCOMPATIBLE:
        return (INCOMPATIBLE_CODE,), ()
    return (MISSING_CODE,), ()


# ---------------------------------------------------------------------------
# Classification of the payloads
# ---------------------------------------------------------------------------


def _policy_checks(
    policy: HierarchicalIntegrationPolicy | None,
) -> tuple[int | None, frozenset[str]]:
    """The root boundary's numeric producer and check names; only root candidates become main."""
    if policy is None:
        return None, frozenset()
    required = policy.root.required_checks
    if not is_numeric_producer_id(required.producer_id):
        return None, frozenset()
    return int(required.producer_id), frozenset(required.names)


def _pinned(value: Any) -> int | None:
    return value if type(value) is int else None


def _status_checks(
    checks: Any,
    *,
    pin_key: str,
    app_id: int | None,
    producer: int | None,
    names: frozenset[str],
    attestation_name: str = ATTESTATION_NAME,
) -> tuple[str | None, bool]:
    """``(refusal, requires_attestation)`` for one set of required checks.

    A check passes an attested fast-forward only when it is the attestation
    pinned to the App id or a policy check pinned to the CI producer.  An
    unpinned context is satisfied by anyone who can post a status, so it
    counts as neither.
    """
    if not isinstance(checks, list) or not all(isinstance(check, dict) for check in checks):
        raise ProtectionUnreadable("required status checks are malformed")
    requires_attestation = False
    unsatisfied: list[str] = []
    for check in checks:
        context = check.get("context")
        if not isinstance(context, str):
            raise ProtectionUnreadable("a required status check has no context")
        pinned = _pinned(check.get(pin_key))
        if context == attestation_name and app_id is not None and pinned == app_id:
            requires_attestation = True
        elif context in names and producer is not None and pinned == producer:
            continue
        else:
            unsatisfied.append(context if pinned is None else f"{context} (app {pinned})")
    refusal = None
    if unsatisfied:
        refusal = (
            "requires checks that are neither the attestation pinned to the App nor a policy "
            "check pinned to the CI producer: " + ", ".join(unsatisfied)
        )
    return refusal, requires_attestation


def _ruleset_rule(
    entry: Any,
    bypass: Mapping[int, str],
    ruleset_names: Mapping[int, str],
    *,
    app_id: int | None,
    producer: int | None,
    names: frozenset[str],
    attestation_name: str = ATTESTATION_NAME,
) -> ProtectionRule:
    if not isinstance(entry, dict):
        raise ProtectionUnreadable("an effective rule is malformed")
    rule_type, ruleset_id = entry.get("type"), entry.get("ruleset_id")
    if not isinstance(rule_type, str) or type(ruleset_id) is not int:
        raise ProtectionUnreadable("an effective rule has no type or ruleset id")
    if ruleset_id not in bypass:
        raise ProtectionUnreadable(f"ruleset {ruleset_id}'s bypass was not read")
    source_type, source = entry.get("ruleset_source_type"), entry.get("ruleset_source")
    fields: dict[str, Any] = {
        "source": "ruleset",
        "type": rule_type,
        "bypass": bypass[ruleset_id],
        "ruleset_id": ruleset_id,
        "ruleset_source_type": source_type if isinstance(source_type, str) else None,
        "ruleset_source": source if isinstance(source, str) else None,
        "ruleset_name": ruleset_names.get(ruleset_id),
    }
    if rule_type in SATISFIED_RULES:
        return ProtectionRule(**fields)
    if rule_type == "required_status_checks":
        parameters = entry.get("parameters")
        if not isinstance(parameters, dict):
            raise ProtectionUnreadable("a required status check rule has no parameters")
        checks = parameters.get("required_status_checks")
        refusal, requires_attestation = _status_checks(
            checks, pin_key="integration_id", app_id=app_id, producer=producer, names=names,
            attestation_name=attestation_name,
        )
        return ProtectionRule(
            **fields,
            refusal=refusal,
            requires_attestation=requires_attestation,
            refuses_unattested=bool(checks),
        )
    refusal = REFUSING_RULES.get(
        rule_type, f"rule type {rule_type!r} is not known to accept an attested fast-forward"
    )
    return ProtectionRule(**fields, refusal=refusal, refuses_unattested=True)


def _enabled(setting: Any) -> bool:
    return isinstance(setting, dict) and setting.get("enabled") is True


def _lists_app(allowances: Any, app_id: int | None) -> bool:
    apps = allowances.get("apps") if isinstance(allowances, dict) else None
    return (
        app_id is not None
        and isinstance(apps, list)
        and any(isinstance(app, dict) and _pinned(app.get("id")) == app_id for app in apps)
    )


def _classic_rules(
    classic: Any,
    *,
    app_id: int | None,
    producer: int | None,
    names: frozenset[str],
    attestation_name: str = ATTESTATION_NAME,
) -> list[ProtectionRule]:
    """Classic branch protection as rules; ``None`` (a 404) is none."""
    if classic is None:
        return []
    if not isinstance(classic, dict):
        raise ProtectionUnreadable("classic branch protection is malformed")

    def rule(rule_type: str, **fields: Any) -> ProtectionRule:
        return ProtectionRule(source="classic", type=rule_type, bypass=CLASSIC_BYPASS, **fields)

    rules: list[ProtectionRule] = []
    status = classic.get("required_status_checks")
    if status is not None:
        if not isinstance(status, dict):
            raise ProtectionUnreadable("classic required status checks are malformed")
        checks = status.get("checks")
        if checks is None:
            # Only the legacy ``contexts`` list: every entry is unpinned.
            contexts = status.get("contexts") or []
            if not isinstance(contexts, list):
                raise ProtectionUnreadable("classic required contexts are malformed")
            checks = [{"context": context} for context in contexts]
        refusal, requires_attestation = _status_checks(
            checks, pin_key="app_id", app_id=app_id, producer=producer, names=names,
            attestation_name=attestation_name,
        )
        if checks:
            rules.append(
                rule(
                    "required_status_checks",
                    refusal=refusal,
                    requires_attestation=requires_attestation,
                    refuses_unattested=True,
                )
            )
    reviews = classic.get("required_pull_request_reviews")
    if reviews is not None and not _lists_app(
        reviews.get("bypass_pull_request_allowances") if isinstance(reviews, dict) else None,
        app_id,
    ):
        rules.append(rule("pull_request", refusal=REFUSING_RULES["pull_request"],
                          refuses_unattested=True))
    for setting in ("required_signatures", "required_linear_history", "lock_branch"):
        if _enabled(classic.get(setting)):
            rules.append(rule(setting, refusal=REFUSING_RULES[setting], refuses_unattested=True))
    restrictions = classic.get("restrictions")
    if restrictions is not None and not _lists_app(restrictions, app_id):
        rules.append(rule("restrictions", refusal=REFUSING_RULES["restrictions"],
                          refuses_unattested=True))
    return rules


def classify(
    effective: Any,
    bypass: Mapping[int, str],
    classic: Any,
    *,
    app_id: int | None,
    policy: HierarchicalIntegrationPolicy | None = None,
    ruleset_names: Mapping[int, str] | None = None,
    attestation_name: str = ATTESTATION_NAME,
) -> ProtectionReading:
    """Classify the three reads for the App's promotion push (spec §8.3).

    *effective* is the ``rules/branches`` list, *bypass* each of its rulesets'
    ``current_user_can_bypass``, and *classic* the classic protection object
    (``None`` for a 404).  *ruleset_names* only labels the rules.  Anything
    malformed is ``unverifiable``.
    """
    producer, names = _policy_checks(policy)
    try:
        if not isinstance(effective, list):
            raise ProtectionUnreadable("the effective rules are not a list")
        for ruleset_id, value in bypass.items():
            if value not in BYPASS_VALUES:
                raise ProtectionUnreadable(
                    f"ruleset {ruleset_id} reported current_user_can_bypass {value!r}"
                )
        rules = [
            _ruleset_rule(
                entry, bypass, ruleset_names or {}, app_id=app_id, producer=producer, names=names,
                attestation_name=attestation_name,
            )
            for entry in effective
        ]
        rules += _classic_rules(
            classic, app_id=app_id, producer=producer, names=names,
            attestation_name=attestation_name,
        )
    except ProtectionUnreadable as exc:
        return ProtectionReading(UNVERIFIABLE, reason=str(exc))

    rules_tuple = tuple(rules)
    enforced = [rule for rule in rules_tuple if not rule.bypassed]
    refusing = [rule for rule in enforced if rule.refusal is not None]
    if refusing:
        reason = "; ".join(
            f"{_rule_label(rule)}: {rule.refusal}" for rule in refusing
        )
        return ProtectionReading(INCOMPATIBLE, rules_tuple, reason)
    if any(rule.requires_attestation for rule in enforced):
        return ProtectionReading(ATTESTED_ONLY, rules_tuple)
    gating = [rule for rule in rules_tuple if rule.refuses_unattested]
    if gating and all(rule.bypassed for rule in gating):
        return ProtectionReading(APP_BYPASS, rules_tuple)
    return ProtectionReading(UNPROTECTED, rules_tuple)


def _rule_label(rule: ProtectionRule) -> str:
    if rule.source == "classic":
        return f"classic protection {rule.type}"
    origin = "organization ruleset" if rule.ruleset_source_type == "Organization" else "ruleset"
    return f"{origin} {rule.ruleset_id} {rule.type}"


# ---------------------------------------------------------------------------
# Reads through the App
# ---------------------------------------------------------------------------


def _describe(exc: BaseException) -> str:
    # GitHubAccessError carries a classification and no raw diagnostics.
    if isinstance(exc, GitHubAccessError):
        return f"{exc.category}: {exc}"
    if isinstance(exc, ValueError):
        return str(exc)
    return type(exc).__name__


async def read_protection(
    client: Any,
    binding: Any,
    default_branch: str | None,
    *,
    app_id: int | None,
    policy: HierarchicalIntegrationPolicy | None = None,
    attestation_name: str = ATTESTATION_NAME,
) -> ProtectionReading:
    """Read and classify the default branch's protection through *client*.

    Every path is bound to the numeric repository id, so a rename between
    reads cannot redirect one.  Any failed read is ``unverifiable``.
    """
    stage = "the effective rules"
    try:
        if not default_branch:
            raise ProtectionUnreadable("the repository has no default branch")
        root = f"/repositories/{binding.repository_id}"
        branch = quote(default_branch, safe="")
        effective = await client.paged_list(
            f"{root}/rules/branches/{branch}?per_page=100", max_pages=MAX_RULE_PAGES
        )
        ruleset_ids = sorted(
            {
                entry["ruleset_id"]
                for entry in effective
                if isinstance(entry, dict) and type(entry.get("ruleset_id")) is int
            }
        )
        bypass: dict[int, str] = {}
        ruleset_names: dict[int, str] = {}
        for ruleset_id in ruleset_ids:
            stage = f"ruleset {ruleset_id}"
            ruleset = await client.request_json("GET", f"{root}/rulesets/{ruleset_id}")
            if ruleset.get("id") != ruleset_id:
                raise ProtectionUnreadable(f"ruleset {ruleset_id} answered another id")
            value = ruleset.get("current_user_can_bypass")
            if value not in BYPASS_VALUES:
                # Documented for installation tokens, unproven on this install
                # (spec §10 S2): an absent value is never guessed.
                raise ProtectionUnreadable(
                    f"ruleset {ruleset_id} did not report current_user_can_bypass"
                )
            bypass[ruleset_id] = value
            if isinstance(ruleset.get("name"), str):
                ruleset_names[ruleset_id] = ruleset["name"]
        stage = "classic branch protection"
        try:
            classic = await client.request_json("GET", f"{root}/branches/{branch}/protection")
        except GitHubAccessError as exc:
            if exc.category != "not_found_or_hidden":
                raise
            classic = None
    except Exception as exc:  # noqa: BLE001 - every failed read is unverifiable
        return ProtectionReading(UNVERIFIABLE, reason=f"{stage} not read: {_describe(exc)}")
    return classify(
        effective, bypass, classic, app_id=app_id, policy=policy, ruleset_names=ruleset_names,
        attestation_name=attestation_name,
    )


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


BindingResolver = Callable[[Any], Any]
ClientFactory = Callable[[Any], Any]


async def _app_client(
    repository: Any, binding_resolver: BindingResolver, client_factory: ClientFactory
) -> tuple[Any, Any, Any] | None:
    """``(binding, client, identity)`` for *repository*; ``None`` without a GitHub binding.

    A resolver or factory that raises propagates: the caller cannot tell
    whether the repository is protected.
    """
    binding = await _resolve(binding_resolver(repository))
    if binding is None:
        return None
    client = await _resolve(client_factory(binding))
    return binding, client, credential_identity_from_client(client)


def bound_policy(project: Any) -> HierarchicalIntegrationPolicy | None:
    """*project*'s hierarchical policy; ``None`` when it has none (development binds its own)."""
    try:
        return HierarchicalIntegrationPolicy.model_validate(
            getattr(project, "hierarchical_integration_policy", None)
        )
    except (ValidationError, TypeError, ValueError):
        return None


def enablement_reader(
    db: Any, *, binding_resolver: BindingResolver, client_factory: ClientFactory
) -> Callable[[str], Awaitable[bool | tuple[str, ...]]]:
    """The attestation service's ``protection_reader``: the strict modes' blockers.

    ``True`` when the repository's protection is what App publication needs,
    otherwise the §8.3 blocker codes.  The coupling is App mode's (§8.2), so
    existing-login credentials read ``True``.
    """

    async def read(repository_id: str) -> bool | tuple[str, ...]:
        try:
            repository = await db.get_repo(repository_id)
            bound = (
                await _app_client(repository, binding_resolver, client_factory)
                if repository is not None
                else None
            )
            if bound is not None and bound[2].mode is GitHubCredentialMode.APP:
                project = await db.get_project(repository.project_id)
        except Exception:  # noqa: BLE001 - nothing read is nothing verified
            return (UNVERIFIABLE_CODE,)
        if bound is None:
            return (UNVERIFIABLE_CODE,)
        binding, client, identity = bound
        if identity.mode is not GitHubCredentialMode.APP:
            return True
        reading = await read_protection(
            client,
            binding,
            repository.default_branch,
            app_id=identity.app_id,
            policy=bound_policy(project),
        )
        failures, _warnings = judge(reading, None)
        return failures or True

    return read


class DevelopmentPublisherBlocked(ValueError):
    """Development refused: the App's unattested push would be refused (spec §8.2)."""

    code = BLOCKS_DEVELOPMENT_CODE

    def __init__(self, repository_id: str, app_id: int | None, reading: ProtectionReading):
        self.repository_id = repository_id
        self.reading = reading
        blocking = [
            rule for rule in reading.rules if rule.refuses_unattested and not rule.bypassed
        ]
        rulesets = sorted({_rule_label(rule) for rule in blocking if rule.source == "ruleset"})
        classic = sorted({rule.type for rule in blocking if rule.source == "classic"})
        steps = []
        if rulesets:
            steps.append(
                f'add {{"actor_id": {app_id}, "actor_type": "Integration", "bypass_mode": '
                f'"always"}} to the bypass actors of {", ".join(rulesets)} (an organization '
                "ruleset is edited in the organization's settings)"
            )
        if classic:
            steps.append(
                f"remove classic protection's {', '.join(classic)}, which has no App bypass"
            )
        super().__init__(
            f"{BLOCKS_DEVELOPMENT_CODE}: repository {repository_id}'s default branch protection "
            f"is {reading.classification}, and rules the App cannot bypass refuse the "
            f"development publisher's unattested push; {'; '.join(steps)}; then enter "
            "development (runbook §9.6)"
        )

    def blocker(self) -> dict[str, str]:
        return {"code": self.code, "detail": str(self), "ref": self.repository_id}


async def development_guard(
    repository: Any,
    *,
    binding_resolver: BindingResolver,
    client_factory: ClientFactory,
    identity: GitHubCredentialIdentity | None = None,
    policy: HierarchicalIntegrationPolicy | None = None,
) -> ProtectionReading | None:
    """Refuse the development publisher while the App cannot push unattested.

    Raises :class:`DevelopmentPublisherBlocked` under ``attested_only``,
    ``incompatible``, or any other rule the App cannot bypass that refuses an
    unattested push.  Returns the reading otherwise.  ``None`` means the
    coupling does not apply: existing-login credentials (*identity*, the
    daemon's, is checked before any GitHub call) or no GitHub binding (a local
    remote).  An unverifiable reading shows nothing that blocks the publisher:
    on a repository whose plan offers no rulesets or branch protection, the
    reads fail and nothing can refuse the push.  It is returned for the caller
    to report, not refused.  *policy*, the project's hierarchical policy when
    it still has one, only names the CI producer's checks correctly.
    """
    if identity is not None and identity.mode is not GitHubCredentialMode.APP:
        return None
    try:
        bound = await _app_client(repository, binding_resolver, client_factory)
    except Exception as exc:  # noqa: BLE001 - reported as the unverifiable reading
        return ProtectionReading(UNVERIFIABLE, reason=f"the App client: {_describe(exc)}")
    if bound is None:
        return None
    binding, client, bound_identity = bound
    if bound_identity.mode is not GitHubCredentialMode.APP:
        return None
    reading = await read_protection(
        client, binding, repository.default_branch, app_id=bound_identity.app_id, policy=policy
    )
    if reading.development_publisher_blocked:
        raise DevelopmentPublisherBlocked(repository.id, bound_identity.app_id, reading)
    return reading


__all__ = [
    "APP_BYPASS",
    "APP_BYPASS_CODE",
    "ATTESTED_ONLY",
    "BLOCKS_DEVELOPMENT_CODE",
    "BYPASSING",
    "BYPASS_VALUES",
    "CLASSIFICATIONS",
    "INCOMPATIBLE",
    "INCOMPATIBLE_CODE",
    "MISSING_CODE",
    "UNPROTECTED",
    "UNVERIFIABLE",
    "UNVERIFIABLE_CODE",
    "DevelopmentPublisherBlocked",
    "ProtectionReading",
    "ProtectionRule",
    "bound_policy",
    "classify",
    "development_guard",
    "enablement_reader",
    "judge",
    "read_protection",
    "required_classification",
]
