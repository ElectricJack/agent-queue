"""App-mode readiness: the one item report behind ``app-verify`` and the preflight.

App credential mode (``integration.github_app``) depends on the daemon's App
credential and on repository-side trust anchors that nothing in AQ writes: the
trust manifest, two Actions variables, the default branch's protection and the
``main`` push audit workflow (App-mode integration train spec §6.2).
:func:`evaluate` checks each of them once and returns one :class:`AppModeItem`
per concern: a status (``ok``, ``warn`` or ``fail``), a code, what was
expected, what GitHub showed, and the exact fix.

Two callers read the same report, so they cannot disagree:

* ``integration_app_verify`` returns it as it is;
* ``daemon_functional_preflight`` turns every ``fail`` code into a blocker of
  the same name and every ``warn`` code into a non-blocking warning, which
  ``aq integration status`` shows.

Every GitHub read goes through the daemon's App client and is read-only.
Writing an anchor is the operator's: ``aq integration app-setup --apply`` runs
``gh`` with their login, because the App never holds that permission (spec I5).
"""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from pydantic import ValidationError

from src.git.github_contracts import (
    GitHubAccessError,
    GitHubCredentialIdentity,
    GitHubCredentialMode,
)
from src.integration import protection, trust_manifest
from src.integration.ci import TRUST_MANIFEST_PATH
from src.integration.models import HierarchicalIntegrationPolicy

OK = "ok"
WARN = "warn"
FAIL = "fail"

#: One item per concern, in report order (spec §6.2).
ITEM_IDS = (
    "credential",
    "repository",
    "producer",
    "manifest",
    "variables",
    "protection",
    "audit_workflow",
)
#: Items that can only warn: a missing audit never blocks readiness.
WARN_ONLY = frozenset({"audit_workflow"})

APP_ID_VARIABLE = "AQ_INTEGRATION_ATTESTATION_APP_ID"
CHECK_VERSION_VARIABLE = "AQ_INTEGRATION_REQUIRED_CHECK_VERSION"
HOSTED_VARIABLES = (APP_ID_VARIABLE, CHECK_VERSION_VARIABLE)

WORKFLOWS_PATH = ".github/workflows"
AUDIT_WORKFLOW_PATH = ".github/workflows/main-attestation.yml"
#: What makes a workflow the hosted consumer (spec §7.2): it reads the App id.
AUDIT_VARIABLE_REFERENCE = f"vars.{APP_ID_VARIABLE}"
#: Workflow files read while looking for the audit; bounds a pathological directory.
MAX_WORKFLOW_FILES = 50


@dataclass(frozen=True)
class AppModeItem:
    """One concern's verdict.  ``codes`` holds every code of ``status``."""

    id: str
    status: str
    codes: tuple[str, ...] = ()
    expected: Any = None
    observed: Any = None
    fix: str | None = None

    @property
    def code(self) -> str | None:
        return self.codes[0] if self.codes else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "code": self.code,
            "codes": list(self.codes),
            "expected": self.expected,
            "observed": self.observed,
            "fix": self.fix,
        }


@dataclass(frozen=True)
class AppModeReport:
    items: tuple[AppModeItem, ...]
    #: The manifest text, the two variable values and the target ruleset.
    expected: dict[str, Any] = field(default_factory=dict)

    @property
    def blockers(self) -> tuple[str, ...]:
        """Every ``fail`` code, in item order: the preflight's blockers."""
        return tuple(
            dict.fromkeys(
                code for item in self.items if item.status == FAIL for code in item.codes
            )
        )

    @property
    def warnings(self) -> tuple[str, ...]:
        """Every ``warn`` code that is not already a blocker."""
        blockers = set(self.blockers)
        return tuple(
            dict.fromkeys(
                code
                for item in self.items
                if item.status == WARN
                for code in item.codes
                if code not in blockers
            )
        )

    @property
    def ready(self) -> bool:
        return not self.blockers

    def item(self, item_id: str) -> AppModeItem | None:
        return next((item for item in self.items if item.id == item_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "items": [item.as_dict() for item in self.items],
            "expected": self.expected,
        }


@dataclass(frozen=True)
class AppModeContext:
    """What one evaluation reads.  Every identity comes from the daemon."""

    project_id: str
    #: AQ's repository id: the manifest's ``canonical_repository_id``.
    repository_id: str
    #: AQ's record of the default branch; GitHub's must equal it.
    default_branch: str | None
    #: The authenticated :class:`~src.git.github_contracts.GitHubRepositoryBinding`.
    binding: Any
    #: The daemon's GitHub client bound to ``binding``.
    client: Any
    identity: GitHubCredentialIdentity
    #: ``None`` only in the preflight, whose database checks already name an
    #: absent or invalid policy; the policy's items are then left out.
    policy: HierarchicalIntegrationPolicy | None
    #: The project mode whose protection requirement applies (spec §8.2);
    #: ``None`` for the strict modes the preflight gates.
    mode: str | None = None
    #: The command's own inputs, repeated in every fix command it prints.
    policy_path: str | None = None
    repository_arg: str | None = None
    promotion_flow: list[dict] | None = None


def required_protection(mode: str | None) -> str | None:
    """The protection classification a project mode needs (spec §8.2); ``None`` is any."""
    return protection.required_classification(mode)


def target_ruleset_name(default_branch: str | None) -> str:
    return f"Train-only {default_branch}"


def target_ruleset(app_id: int, default_branch: str) -> dict[str, Any]:
    """The default branch's ruleset in train mode (spec §8.1), for this App.

    The attestation is required pinned to the App's integration id, so only the
    holder of the daemon's private key can satisfy it, and nothing bypasses it.
    """
    return {
        "name": target_ruleset_name(default_branch),
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": [f"refs/heads/{default_branch}"], "exclude": []}},
        "rules": [
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [
                        {"context": trust_manifest.ATTESTATION_NAME, "integration_id": app_id}
                    ],
                },
            }
        ],
        "bypass_actors": [],
    }


def aq_command(ctx: AppModeContext, verb: str, *extra: str) -> str:
    """``aq integration VERB PROJECT`` with the evaluation's own inputs."""
    parts = ["aq", "integration", verb, ctx.project_id]
    if ctx.policy_path:
        parts += ["--policy", ctx.policy_path]
    if ctx.repository_arg:
        parts += ["--repository-id", ctx.repository_arg]
    return shlex.join([*parts, *extra])


def _describe(exc: BaseException) -> str:
    # GitHubAccessError carries a classification and no raw diagnostics.
    if isinstance(exc, GitHubAccessError):
        return f"{exc.category}: {exc}"
    if isinstance(exc, ValueError):
        return str(exc)
    return type(exc).__name__


def decoded_content(payload: dict[str, Any]) -> bytes:
    """The bytes of a contents-API file response."""
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise ValueError("content response is malformed")
    encoded = "".join(payload["content"].split())
    return base64.b64decode(encoded, validate=True)


async def read_file_at(client: Any, binding: Any, path: str, sha: str) -> bytes | None:
    """One file at an exact commit, through the App; ``None`` when it is absent there.

    The path is bound to the numeric repository id, so a rename between reads
    cannot redirect it.
    """
    try:
        payload = await client.request_json(
            "GET",
            f"/repositories/{binding.repository_id}/contents/{quote(path, safe='/')}?ref={sha}",
        )
    except GitHubAccessError as exc:
        # The ref read proved the repository visible, so a 404 is the file.
        if exc.category == "not_found_or_hidden":
            return None
        raise
    return decoded_content(payload)


class _DefaultBranch:
    """The default branch's SHA, resolved once so every item reads the same tree."""

    def __init__(self, ctx: AppModeContext) -> None:
        self._ctx = ctx
        self._sha: str | None = None
        self._error: Exception | None = None
        self._resolved = False

    async def sha(self) -> str:
        if not self._resolved:
            self._resolved = True
            try:
                self._sha = await self._resolve()
            except Exception as exc:  # noqa: BLE001 - re-raised to every reader below
                self._error = exc
        if self._error is not None:
            raise self._error
        assert self._sha is not None
        return self._sha

    async def _resolve(self) -> str:
        branch = self._ctx.default_branch
        if not branch:
            raise ValueError("the repository has no default branch")
        sha = await self._ctx.client.exact_head_ref(branch)
        if sha is None:
            raise ValueError(f"default branch {branch!r} does not exist")
        return sha


def _unchecked(item_id: str, cause: AppModeItem) -> AppModeItem:
    """An item that needs the App client, after the credential item failed.

    It carries the credential's code, so the cause is named once and a
    dependent item never claims a finding it could not read.
    """
    return AppModeItem(
        id=item_id,
        status=WARN if item_id in WARN_ONLY else FAIL,
        codes=cause.codes,
        observed={"checked": False, "reason": f"not checked: {cause.id} is {cause.code}"},
        fix=cause.fix,
    )


async def _credential(ctx: AppModeContext) -> AppModeItem:
    identity = ctx.identity
    expected = {"mode": GitHubCredentialMode.APP.value}
    observed: dict[str, Any] = {
        "mode": identity.mode.value,
        "app_id": identity.app_id,
        "installation_id": identity.installation_id,
    }
    if identity.mode is not GitHubCredentialMode.APP:
        return AppModeItem(
            "credential",
            FAIL,
            ("not_app_mode",),
            expected=expected,
            observed=observed,
            fix=(
                "configure integration.github_app (app_id, installation_id, private_key_path) "
                "in ~/.agent-queue/config.yaml and restart the daemon (runbook §9.1)"
            ),
        )
    try:
        # The mint enforces the exact permission set and this repository.
        token = await ctx.client.installation_token()
    except Exception as exc:  # noqa: BLE001 - any mint failure is the named item
        token = None
        observed["error"] = _describe(exc)
    if not token:
        return AppModeItem(
            "credential",
            FAIL,
            ("app_token_unavailable",),
            expected=expected,
            observed={"token": False, **observed},
            fix=(
                f"install App {identity.app_id} on {ctx.binding.full_name} with the requested "
                "permissions and check integration.github_app's private key (owner-only 0600) "
                "(runbook §9.1)"
            ),
        )
    return AppModeItem(
        "credential", OK, expected=expected, observed={"token": True, **observed}
    )


async def _repository(ctx: AppModeContext) -> AppModeItem:
    binding = ctx.binding
    expected = {
        "id": binding.repository_id,
        "full_name": binding.full_name,
        "default_branch": ctx.default_branch,
    }
    fix = (
        f"make repository {ctx.repository_id}'s origin and default branch name the GitHub "
        "repository the App reads, or designate the right one (runbook §9.3 step 5)"
    )
    try:
        remote = await ctx.client.request_json("GET", f"/repos/{binding.full_name}")
    except Exception as exc:  # noqa: BLE001 - an unread repository cannot be confirmed
        return AppModeItem(
            "repository", FAIL, ("repository_mismatch",), expected=expected,
            observed={"error": _describe(exc)}, fix=fix,
        )
    observed = {name: remote.get(name) for name in ("id", "full_name", "default_branch")}
    if type(observed["id"]) is not int or observed != expected:
        return AppModeItem(
            "repository", FAIL, ("repository_mismatch",), expected=expected,
            observed=observed, fix=fix,
        )
    return AppModeItem("repository", OK, expected=expected, observed=observed)


def _producer(ctx: AppModeContext) -> AppModeItem:
    assert ctx.policy is not None
    observed = {
        "parent": ctx.policy.parent.required_checks.producer_id,
        "root": ctx.policy.root.required_checks.producer_id,
    }
    try:
        producer = trust_manifest.policy_producer_app_id(ctx.policy)
    except trust_manifest.TrustManifestRefusal as exc:
        return AppModeItem(
            "producer",
            FAIL,
            (exc.code,),
            expected="one numeric CI producer App id on both boundaries "
            '(GitHub Actions is "15368")',
            observed=observed,
            fix=(
                f"Review the numeric producer in the policy, then use aq project set "
                f"{shlex.quote(ctx.project_id)} integration-policy POLICY_JSON "
                "--expected-integration-generation GENERATION --reason REASON"
            ),
        )
    return AppModeItem("producer", OK, expected=str(producer), observed=observed)


async def _manifest(
    ctx: AppModeContext, producer: AppModeItem, branch: _DefaultBranch
) -> tuple[AppModeItem, dict[str, Any] | None]:
    """The default-branch manifest, compared on identity; the check set only warns."""
    assert ctx.policy is not None
    required = ctx.policy.root.required_checks
    # The producer item names a producer fault; every other field is still compared.
    known = int(producer.expected) if producer.status == OK else None
    fix = (
        f"{aq_command(ctx, 'trust-manifest', '--write', TRUST_MANIFEST_PATH)}, then commit "
        f"{TRUST_MANIFEST_PATH} to {ctx.default_branch} (runbook §9.2 step 3)"
    )
    try:
        expected = trust_manifest.build_trust_manifest(
            canonical_repository_id=ctx.repository_id,
            repository_id=ctx.binding.repository_id,
            full_name=ctx.binding.full_name,
            ci_producer_app_id=known,
            attestation_app_id=ctx.identity.app_id,
            checks=required.names,
            check_version=required.version,
            promotion_attestation_names=trust_manifest.flow_attestation_names(
                ctx.promotion_flow or ()
            ),
        )
    except trust_manifest.TrustManifestRefusal as exc:
        return (
            AppModeItem("manifest", FAIL, (exc.code,), observed={"error": str(exc)},
                        fix="review the stored promotion flow's attestation identities"),
            None,
        )
    except ValidationError as exc:
        # The App would be its own producer: no manifest can express the policy.
        return (
            AppModeItem(
                "manifest", FAIL, ("trust_manifest_invalid",),
                observed={"error": f"the expected manifest does not validate: {exc}"},
                fix="the policy's producer must not be the daemon's own App",
            ),
            None,
        )

    observed: dict[str, Any] = {"path": TRUST_MANIFEST_PATH, "ref": ctx.default_branch}
    try:
        sha = await branch.sha()
        observed["sha"] = sha
        raw = await read_file_at(ctx.client, ctx.binding, TRUST_MANIFEST_PATH, sha)
    except Exception as exc:  # noqa: BLE001 - an unread manifest is unavailable
        comparison = trust_manifest.compare(expected, None)
        observed.update(comparison.as_dict(), error=f"not read: {_describe(exc)}")
    else:
        comparison = trust_manifest.compare(
            expected, raw, ignore=() if known is not None else ("ci_producer_app_id",)
        )
        observed.update(comparison.as_dict())
    for key in ("status", "code", "warnings"):
        observed.pop(key, None)
    if comparison.status == FAIL:
        item = AppModeItem(
            "manifest", FAIL, (comparison.code,), expected=expected, observed=observed, fix=fix
        )
    elif comparison.status == WARN:
        item = AppModeItem(
            "manifest", WARN, comparison.warnings, expected=expected, observed=observed, fix=fix
        )
    else:
        item = AppModeItem("manifest", OK, expected=expected, observed=observed)
    return item, expected if known is not None else None


async def _variables(ctx: AppModeContext) -> AppModeItem:
    """Both Actions variables, against the authority only (spec §6.3).

    The App id is the daemon's and the version the bound policy root's; the
    manifest's version may lag during a rotation, so it is not consulted.
    """
    assert ctx.policy is not None
    expected = expected_variables(ctx.identity.app_id, ctx.policy)
    values: dict[str, str | None] = {}
    unavailable: dict[str, str] = {}
    for name in HOSTED_VARIABLES:
        values[name] = None
        try:
            payload = await ctx.client.request_json(
                "GET", f"/repos/{ctx.binding.full_name}/actions/variables/{name}"
            )
        except GitHubAccessError as exc:
            unavailable[name] = (
                "absent" if exc.category == "not_found_or_hidden" else _describe(exc)
            )
            continue
        except Exception as exc:  # noqa: BLE001 - an unread variable is unavailable
            unavailable[name] = _describe(exc)
            continue
        if payload.get("name") != name or not isinstance(payload.get("value"), str):
            unavailable[name] = "malformed response"
            continue
        values[name] = payload["value"]
    observed: dict[str, Any] = {"values": values}
    if unavailable:
        observed["unavailable"] = unavailable
    fix = f"{aq_command(ctx, 'app-setup', '--apply')} (runbook §9.3 step 3)"
    if unavailable:
        return AppModeItem(
            "variables", FAIL, ("hosted_workflow_variables_unavailable",),
            expected=expected, observed=observed, fix=fix,
        )
    if values != expected:
        return AppModeItem(
            "variables", FAIL, ("hosted_workflow_variables_mismatch",),
            expected=expected, observed=observed, fix=fix,
        )
    return AppModeItem("variables", OK, expected=expected, observed=observed)


def expected_variables(
    app_id: int | None, policy: HierarchicalIntegrationPolicy
) -> dict[str, str | None]:
    return {
        APP_ID_VARIABLE: str(app_id) if app_id is not None else None,
        CHECK_VERSION_VARIABLE: policy.root.required_checks.version,
    }


async def check_protection(ctx: AppModeContext) -> AppModeItem:
    """The default branch's protection, classified for the App and judged for the mode.

    Spec §8.2-8.3 (:mod:`src.integration.protection`).  An unreadable
    protection is ``unverifiable``, a blocker that never counts as unprotected.

    A disabled project is judged like the strict modes.  Nothing pushes its
    default branch, but it is being readied for them: the preflight that
    gates ``enable`` and ``status`` judges it so, and this item must agree
    (runbook §9.3 steps 3-5 confirm ``attested_only`` while disabled).
    """
    mode = None if ctx.mode == "disabled" else ctx.mode
    reading = await protection.read_protection(
        ctx.client,
        ctx.binding,
        ctx.default_branch,
        app_id=ctx.identity.app_id,
        policy=ctx.policy,
    )
    failures, warnings = protection.judge(reading, mode)
    expected = {"classification": required_protection(mode)}
    observed = reading.as_dict()
    # The repository ruleset expected.ruleset replaces (``app-setup`` prints
    # its PUT): only one already named like the target, never a ruleset that
    # also carries other rules or branches.
    target = target_ruleset_name(ctx.default_branch)
    named = {
        rule.ruleset_id
        for rule in reading.rules
        if rule.ruleset_name == target and rule.ruleset_source_type == "Repository"
    }
    observed["ruleset_id"] = named.pop() if len(named) == 1 else None
    if ctx.promotion_flow and ctx.identity.app_id is not None and ctx.default_branch:
        from src.integration.promotion_steps import FlowSchema, read_promotion_protection

        normalized = FlowSchema.validate(ctx.promotion_flow, default_branch=ctx.default_branch)
        # Trust is judged separately; malformed chains cannot be read as valid protection.
        if any(problem.layer < 3 for problem in normalized.problems):
            observed["promotion"] = {"problems": [p.as_dict() for p in normalized.problems]}
            warnings = (*warnings, "promotion_flow_invalid")
        else:
            chain = await read_promotion_protection(
                ctx.client, ctx.binding, normalized.flow, default_branch=ctx.default_branch,
                app_id=ctx.identity.app_id, policy=ctx.policy,
            )
            observed["promotion"] = chain
            warnings = tuple(dict.fromkeys([*warnings, *(w["code"] for w in chain["warnings"])]))
    if not failures and not warnings:
        return AppModeItem("protection", OK, expected=expected, observed=observed)
    return AppModeItem(
        "protection",
        FAIL if failures else WARN,
        failures or warnings,
        expected=expected,
        observed=observed,
        fix=(f"inspect `aq promote rulesets --project {ctx.project_id}` and apply the "
             "reported rulesets as repository admin" if not failures and ctx.promotion_flow
             else _protection_fix(ctx, (failures or warnings)[0])),
    )


def _protection_fix(ctx: AppModeContext, code: str) -> str:
    app_id = ctx.identity.app_id
    if code == protection.UNVERIFIABLE_CODE:
        return (
            f"let App {app_id} read {ctx.binding.full_name}'s rulesets and branch protection "
            f"(administration: read; observed.reason names the failed read), then re-run "
            f"{aq_command(ctx, 'app-verify')}"
        )
    if code == protection.BLOCKS_DEVELOPMENT_CODE:
        return (
            f'add {{"actor_id": {app_id}, "actor_type": "Integration", "bypass_mode": '
            f'"always"}} to the bypass actors of every ruleset in observed.rules that '
            "refuses the App, and remove any classic protection that does "
            "(runbook §9.6 rollback to development)"
        )
    fix = (
        f"apply expected.ruleset from {aq_command(ctx, 'app-verify')} --json "
        "(runbook §9.3 step 4)"
    )
    if code == protection.INCOMPATIBLE_CODE:
        fix += ", and remove the rules observed.reason names"
    elif code == protection.APP_BYPASS_CODE:
        fix += "; it has no bypass actors"
    return fix


async def _audit_workflow(ctx: AppModeContext, branch: _DefaultBranch) -> AppModeItem:
    """A default-branch workflow that reads the attestation App id (spec §7.2)."""
    expected = {"reads": AUDIT_VARIABLE_REFERENCE, "path": AUDIT_WORKFLOW_PATH}
    fix = f"commit {AUDIT_WORKFLOW_PATH} to {ctx.default_branch} (runbook §9.2 step 4)"
    observed: dict[str, Any] = {"ref": ctx.default_branch}
    try:
        sha = await branch.sha()
        observed["sha"] = sha
        entries = await ctx.client.paged_list(
            f"/repositories/{ctx.binding.repository_id}/contents/{WORKFLOWS_PATH}?ref={sha}",
            max_pages=1,
        )
    except GitHubAccessError as exc:
        entries = []
        if exc.category != "not_found_or_hidden":
            observed["error"] = _describe(exc)
    except Exception as exc:  # noqa: BLE001 - an unread directory finds nothing
        entries = []
        observed["error"] = _describe(exc)
    paths = sorted(
        (
            entry["path"]
            for entry in entries
            if entry.get("type") == "file"
            and isinstance(entry.get("path"), str)
            and entry["path"].endswith((".yml", ".yaml"))
        ),
        # The shipped audit first: usually the only read needed.
        key=lambda path: (path != AUDIT_WORKFLOW_PATH, path),
    )[:MAX_WORKFLOW_FILES]
    unread: dict[str, str] = {}
    for path in paths:
        try:
            raw = await read_file_at(ctx.client, ctx.binding, path, observed["sha"])
        except Exception as exc:  # noqa: BLE001 - one unreadable file is skipped
            unread[path] = _describe(exc)
            continue
        if raw is not None and AUDIT_VARIABLE_REFERENCE.encode() in raw:
            return AppModeItem(
                "audit_workflow", OK, expected=expected, observed={**observed, "path": path}
            )
    observed["searched"] = paths
    if unread:
        observed["unread"] = unread
    return AppModeItem(
        "audit_workflow", WARN, ("audit_workflow_missing",),
        expected=expected, observed=observed, fix=fix,
    )


async def evaluate(ctx: AppModeContext) -> AppModeReport:
    """Every App-mode item for one repository, in :data:`ITEM_IDS` order."""
    branch = _DefaultBranch(ctx)
    credential = await _credential(ctx)
    readable = credential.status == OK

    items = [credential]
    items.append(await _repository(ctx) if readable else _unchecked("repository", credential))
    manifest_expected = None
    if ctx.policy is not None:
        producer = _producer(ctx)
        items.append(producer)
        if readable:
            manifest, manifest_expected = await _manifest(ctx, producer, branch)
            items += [manifest, await _variables(ctx)]
        else:
            items += [_unchecked("manifest", credential), _unchecked("variables", credential)]
    items.append(await check_protection(ctx) if readable else _unchecked("protection", credential))
    items.append(
        await _audit_workflow(ctx, branch) if readable else _unchecked("audit_workflow", credential)
    )
    return AppModeReport(tuple(items), expected=_expected(ctx, manifest_expected))


def _expected(ctx: AppModeContext, manifest: dict[str, Any] | None) -> dict[str, Any]:
    app_id = ctx.identity.app_id
    text = trust_manifest.canonical_text(manifest) if manifest is not None else None
    return {
        "manifest_path": TRUST_MANIFEST_PATH,
        "manifest": manifest,
        "manifest_text": text,
        "manifest_sha256": trust_manifest.text_sha256(text) if text is not None else None,
        "variables": (
            expected_variables(app_id, ctx.policy) if ctx.policy is not None else None
        ),
        "ruleset": (
            target_ruleset(app_id, ctx.default_branch)
            if app_id is not None and ctx.default_branch
            else None
        ),
    }


__all__ = [
    "APP_ID_VARIABLE",
    "AUDIT_VARIABLE_REFERENCE",
    "AUDIT_WORKFLOW_PATH",
    "CHECK_VERSION_VARIABLE",
    "FAIL",
    "HOSTED_VARIABLES",
    "ITEM_IDS",
    "OK",
    "WARN",
    "AppModeContext",
    "AppModeItem",
    "AppModeReport",
    "aq_command",
    "check_protection",
    "decoded_content",
    "evaluate",
    "expected_variables",
    "read_file_at",
    "required_protection",
    "target_ruleset",
]
