"""Plan one project's move onto the integration train.

``aq integration onboard-train`` (``src/cli/integration.py``) gathers a
project's facts -- its repository URL and default branch, the workflow files on
that branch, the reviewed route bundles this daemon ships -- and hands them to
:func:`plan_onboarding`.  Everything here is pure: nothing reads Git, GitHub,
the network or the database, so a plan is reproducible from its inputs and the
tests can feed it the real workflows of the projects it was written for.

What the train needs from a repository decides the project's *shape*:

* The CI evidence observer accepts only check runs from a ``push`` workflow run
  on the exact candidate SHA (``AuthenticatedGitHubObserver(expected_event=
  "push")`` in ``src/integration/attestation.py``).  Candidates are pushed to
  ``aq/integration/**`` and parent snapshots to ``aq/parent/**``
  (``src/integration/delivery_branches.py``, ``src/integration/parent_ci.py``),
  so a workflow counts only when its push trigger covers both.
* The required check set is the check-run names those workflows produce, which
  GitHub derives from each job's ``name`` (or id) and its matrix.
* Hierarchy and train run only on github.com (``delivery_path_problems``).  A
  repository on disk or on another forge delivers through the development
  publisher, which is forge-agnostic, with a local validation command.

Shapes: ``github_ci`` (ready to bind), ``github_ci_trigger_missing`` (CI exists
but does not run on the train's refs; a workflow change lands first),
``github_no_ci`` (no CI at all; a workflow lands first, and
:func:`ci_workflow_template` writes a starting point), ``local_remote`` and
``unsupported_forge`` (the development path).  The runbook is
``docs/config/train-onboarding.md``; App credential mode's is
``docs/config/app-mode-train.md``.

In App credential mode a GitHub project also carries the ``main`` push audit
(:func:`audit_workflow_template`): the hosted verifier embedded verbatim, and a
fallback that re-runs the project's gating CI through ``workflow_call`` when
that workflow can be called safely, or else fails with an "unattested push"
summary.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from src.integration.models import HierarchicalIntegrationPolicy, PlaybookRoute

#: Branch names the train pushes and reads CI from.  Samples, not patterns:
#: a workflow's push filter must match a real ref of each kind.
TRAIN_REF_SAMPLES = (
    "aq/integration/sample-project/0123456789ab",
    "aq/parent/sample-task/0123456789abcdef/1/" + "0" * 40,
)
#: The patterns a trigger fix adds to ``on.push.branches``.
TRAIN_BRANCH_PATTERNS = ("aq/integration/**", "aq/parent/**")

#: The GitHub Actions App.  Every policy the planner emits names it by its
#: numeric id, the canonical producer in both credential modes, so a project can
#: move between them without rebinding: App credential mode compares it with the
#: trust manifest's ``ci_producer_app_id`` and refuses a slug as
#: ``ci_producer_not_numeric`` (``src/integration/preflight.py``).
GITHUB_ACTIONS_APP_ID = 15368
#: Legacy only: policies bound before the numeric id was canonical name GitHub
#: Actions by this slug.  Existing-login credentials still match it (frozen
#: snapshots and evidence rows hold it); the planner never emits it.
GITHUB_ACTIONS_SLUG = "github-actions"
ATTESTATION_NAME = "Agent Queue Integration Attestation"
TRUST_MANIFEST_PATH = ".github/agent-queue-integration.json"
AUDIT_WORKFLOW_PATH = ".github/workflows/main-attestation.yml"
#: What marks a workflow as the audit, as ``app-verify``'s ``audit_workflow`` item reads it.
AUDIT_VARIABLE_REFERENCE = "vars.AQ_INTEGRATION_ATTESTATION_APP_ID"
#: The stdlib-only verifier the audit workflow embeds (App-mode spec §7.2).
HOSTED_VERIFIER_PATH = Path(__file__).with_name("hosted_attestation.py")
#: What the audit workflow's token holds, so all a called workflow may ask for:
#: GitHub lets a called workflow narrow the caller's permissions, never widen them.
AUDIT_PERMISSIONS = {"contents": "read", "checks": "read"}

SHARED_PARENT_ROUTE = "parent-integration"
SHARED_ROOT_ROUTE = "root-train"

CREDENTIAL_MODES = ("existing-login", "app")

_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_MATRIX_REF = re.compile(r"^matrix((?:\.[A-Za-z0-9_-]+)+)$")


# ---------------------------------------------------------------------------
# Workflow analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobChecks:
    """The check runs one workflow job produces, and whether the train needs them."""

    job_id: str
    names: tuple[str, ...]
    #: ``required`` | ``excluded`` | ``unresolved``
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class WorkflowReport:
    path: str
    name: str
    push_on_train_refs: bool
    pull_request: bool
    push: bool
    deployment: bool
    jobs: tuple[JobChecks, ...]
    notes: tuple[str, ...] = ()
    #: Declares the ``workflow_call`` trigger.
    workflow_call: bool = False
    #: The App-mode ``main`` push audit (it reads the attestation App id): never CI.
    audit: bool = False
    #: Why the ``main`` push audit cannot call this workflow; ``None`` when it can.
    call_refusal: str | None = "not a readable workflow"

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(name for job in self.jobs if job.status == "required" for name in job.names)

    @property
    def is_ci_candidate(self) -> bool:
        """CI that could gate the train once its push trigger names the train's refs."""
        return (self.pull_request or self.push) and not self.deployment and not self.audit


def _triggers(document: Mapping[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads a bare ``on`` key as boolean true.
    raw = document.get("on", document.get(True))
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(item): None for item in raw}
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    return {}


def _filter_regex(pattern: str) -> re.Pattern[str]:
    """GitHub's branch filter glob: ``**`` crosses ``/``, ``*`` does not."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        if char == "*":
            out.append("[^/]*")
        elif char == "[":
            end = pattern.find("]", index)
            if end < 0:
                out.append(re.escape(char))
            else:
                out.append(pattern[index : end + 1])
                index = end
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("^" + "".join(out) + "$")


def branch_filter_matches(config: Any, branch: str) -> bool:
    """Whether a ``push`` trigger's branch filters run it for ``branch``."""
    if not isinstance(config, Mapping):
        return True
    branches = config.get("branches")
    ignored = config.get("branches-ignore")
    if branches is None and ignored is None:
        # A tags-only push filter never runs for a branch.
        return "tags" not in config and "tags-ignore" not in config
    if isinstance(branches, str):
        branches = [branches]
    if isinstance(ignored, str):
        ignored = [ignored]
    if branches is not None:
        matched = False
        for pattern in branches:
            pattern = str(pattern)
            negated = pattern.startswith("!")
            if _filter_regex(pattern.removeprefix("!")).match(branch):
                matched = not negated
        return matched
    return not any(_filter_regex(str(pattern)).match(branch) for pattern in ignored or ())


def _push_covers_train_refs(triggers: Mapping[str, Any]) -> tuple[bool, str | None]:
    if "push" not in triggers:
        return False, None
    config = triggers["push"]
    if isinstance(config, Mapping) and (config.get("paths") or config.get("paths-ignore")):
        return False, "push trigger is path-filtered, so a candidate may produce no check runs"
    missing = [ref for ref in TRAIN_REF_SAMPLES if not branch_filter_matches(config, ref)]
    if missing:
        return False, None
    return True, None


# Three-valued evaluation of a job ``if:`` for a push of a train ref.  Known:
# ``github.event_name`` (``push``), ``github.ref``/``github.ref_name`` (an
# ``aq/integration/...`` or ``aq/parent/...`` branch, so equal to no literal
# outside ``aq/``) and the status functions; every other atom is unknown.
_TOKEN = re.compile(
    r"\s*(?:(?P<op>&&|\|\||==|!=|!|\(|\)|,)|(?P<str>'(?:[^']|'')*')"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_.\-*]*)|(?P<num>-?\d+(?:\.\d+)?))"
)


_STATUS_FUNCTIONS = {
    "always": "true",
    "success": "true",
    "failure": "false",
    "cancelled": "false",
}
#: A job ``if:`` naming one of these still runs when a job it needs was skipped.
_RUNS_AFTER_SKIPPED_NEED = ("always()", "failure()", "cancelled()")


class _IfParser:
    def __init__(self, text: str, event: str) -> None:
        self.tokens: list[tuple[str, str]] = []
        position = 0
        text = text.strip()
        while position < len(text):
            match = _TOKEN.match(text, position)
            if match is None or match.end() == position:
                raise ValueError(f"cannot read expression at {text[position:]!r}")
            kind = match.lastgroup or "op"
            self.tokens.append((kind, match.group(kind)))
            position = match.end()
        self.index = 0
        self.event = event

    def _peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self.tokens[self.index]
        self.index += 1
        return token

    def parse(self) -> bool | None:
        value = self._or()
        if self._peek() is not None:
            raise ValueError("trailing tokens")
        return value

    def _or(self) -> bool | None:
        value = self._and()
        while self._peek() == ("op", "||"):
            self._take()
            right = self._and()
            value = (
                True
                if value is True or right is True
                else (False if value is False and right is False else None)
            )
        return value

    def _and(self) -> bool | None:
        value = self._not()
        while self._peek() == ("op", "&&"):
            self._take()
            right = self._not()
            value = (
                False
                if value is False or right is False
                else (True if value is True and right is True else None)
            )
        return value

    def _not(self) -> bool | None:
        if self._peek() == ("op", "!"):
            self._take()
            value = self._not()
            return None if value is None else not value
        return self._comparison()

    def _comparison(self) -> bool | None:
        left = self._atom()
        token = self._peek()
        if token in (("op", "=="), ("op", "!=")):
            operator = self._take()[1]
            right = self._atom()
            if right[0] in ("event", "ref", "ref_name") and left[0] == "str":
                left, right = right, left
            if right[0] != "str":
                return None
            if left[0] == "event":
                equal = right[1] == self.event
            elif left[0] == "ref":
                if right[1].startswith("refs/heads/aq/"):
                    return None
                equal = False
            elif left[0] == "ref_name":
                if right[1].startswith("aq/"):
                    return None
                equal = False
            else:
                return None
            return equal if operator == "==" else not equal
        if left[0] == "bool":
            return left[1] == "true"
        return None

    def _atom(self) -> tuple[str, str]:
        kind, value = self._take()
        if kind == "op" and value == "(":
            inner = self._or()
            if self._take() != ("op", ")"):
                raise ValueError("unbalanced parenthesis")
            return (
                ("bool", "true")
                if inner is True
                else (("bool", "false") if inner is False else ("unknown", ""))
            )
        if kind == "str":
            return "str", value[1:-1].replace("''", "'").lower()
        if kind == "ident":
            if self._peek() == ("op", "("):
                depth = 0
                arguments = 0
                while True:
                    token = self._take()
                    if token == ("op", "("):
                        depth += 1
                    elif token == ("op", ")"):
                        depth -= 1
                        if depth == 0:
                            break
                    else:
                        arguments += 1
                # Status functions, relative to the job's own ``needs``, which
                # the caller follows separately: success() is the default.
                if not arguments and value in _STATUS_FUNCTIONS:
                    return "bool", _STATUS_FUNCTIONS[value]
                return "unknown", ""
            if value == "github.event_name":
                return "event", ""
            if value == "github.ref":
                return "ref", ""
            if value == "github.ref_name":
                return "ref_name", ""
            if value in ("true", "false"):
                return "bool", value
            return "unknown", ""
        return "unknown", value


def job_runs_on_push(condition: Any) -> bool | None:
    """Whether a job ``if:`` runs for a push of a train ref; ``None`` when undecidable."""
    if condition is None:
        return True
    if isinstance(condition, bool):
        return condition
    text = str(condition).strip()
    wrapped = _EXPRESSION.fullmatch(text)
    if wrapped:
        text = wrapped.group(1)
    try:
        return _IfParser(text, "push").parse()
    except (ValueError, IndexError):
        return None


def _matrix_combinations(matrix: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    if matrix is None:
        return None, None
    if not isinstance(matrix, Mapping):
        return None, "matrix is computed by an expression"
    base_keys = [key for key in matrix if key not in ("include", "exclude")]
    axes: list[list[Any]] = []
    for key in base_keys:
        values = matrix[key]
        if not isinstance(values, list):
            return None, f"matrix axis {key!r} is computed by an expression"
        axes.append(values)
    combos = (
        [dict(zip(base_keys, values)) for values in itertools.product(*axes)] if base_keys else []
    )
    for excluded in matrix.get("exclude") or ():
        if isinstance(excluded, Mapping):
            combos = [
                combo
                for combo in combos
                if not all(combo.get(key) == value for key, value in excluded.items())
            ]
    for included in matrix.get("include") or ():
        if not isinstance(included, Mapping):
            return None, "matrix include is computed by an expression"
        matched = [
            combo
            for combo in combos
            if all(combo.get(key) == value for key, value in included.items() if key in base_keys)
        ]
        if matched and base_keys:
            for combo in matched:
                for key, value in included.items():
                    if key not in base_keys:
                        combo[key] = value
        else:
            combos.append(dict(included))
    return combos, None


def _lookup(combo: Mapping[str, Any], dotted: str) -> Any:
    value: Any = combo
    for part in dotted.strip(".").split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise KeyError(dotted)
        value = value[part]
    return value


def _render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _substitute_matrix(name: str, combo: Mapping[str, Any]) -> str:
    return _EXPRESSION.sub(
        lambda match: _render(_lookup(combo, match.group(1)[len("matrix") :])), name
    )


def job_check_names(job_id: str, job: Mapping[str, Any]) -> tuple[tuple[str, ...], str | None]:
    """The check-run names GitHub gives ``job``, or a reason they cannot be known."""
    if "uses" in job:
        return (), "calls a reusable workflow; its check names are 'caller / callee'"
    raw_name = job.get("name")
    name = str(raw_name) if raw_name is not None else job_id
    strategy = job.get("strategy") if isinstance(job.get("strategy"), Mapping) else {}
    combos, problem = _matrix_combinations(strategy.get("matrix"))
    if problem:
        return (), problem
    expressions = _EXPRESSION.findall(name)
    matrix_refs = [_MATRIX_REF.match(expression) for expression in expressions]
    if any(match is None for match in matrix_refs):
        return (), "job name uses an expression that is not a matrix value"
    if not combos:
        if expressions:
            return (), "job name reads the matrix but the job has no matrix"
        return (name,), None
    names: list[str] = []
    for combo in combos:
        if expressions:
            try:
                rendered = _substitute_matrix(name, combo)
            except KeyError:
                return (), "job name reads a matrix value some combinations lack"
        else:
            values = list(combo.values())
            if any(isinstance(value, (Mapping, list)) for value in values):
                return (), "matrix values are objects; GitHub's generated name is not derivable"
            rendered = f"{name} ({', '.join(_render(value) for value in values)})"
        names.append(rendered)
    return tuple(names), None


def _empty_report(path: str, note: str) -> WorkflowReport:
    return WorkflowReport(
        path=path,
        name=path,
        push_on_train_refs=False,
        pull_request=False,
        push=False,
        deployment=False,
        jobs=(),
        notes=(note,),
    )


def _needs(job: Mapping[str, Any]) -> list[str]:
    needs = job.get("needs")
    if isinstance(needs, str):
        return [needs]
    return [str(need) for need in needs] if isinstance(needs, list) else []


def _permissions_problem(permissions: Any) -> str | None:
    """What a ``permissions`` block asks beyond the audit's read-only token."""
    if permissions is None:
        return None
    if not isinstance(permissions, Mapping):
        return f"asks for {permissions}"
    wider = [
        f"{scope}: {level}"
        for scope, level in permissions.items()
        if level != "none" and not (level == "read" and AUDIT_PERMISSIONS.get(str(scope)) == "read")
    ]
    return f"asks for {', '.join(wider)}" if wider else None


def _call_refusal(
    triggers: Mapping[str, Any], document: Mapping[str, Any], jobs: Mapping[str, Mapping[str, Any]]
) -> str | None:
    """Why the ``main`` push audit must not call this workflow; ``None`` when it may.

    A called workflow runs with the caller's ref, so a deploy job gated on
    ``main`` would run on every unattested push: a workflow with an
    ``environment`` is never called.  The audit passes no inputs and no
    secrets, and GitHub refuses a called workflow that asks for more token
    permissions than the caller holds, which would stop the audit itself.
    """
    environments = sorted(job_id for job_id, job in jobs.items() if job.get("environment"))
    if environments:
        return f"declares an environment ({', '.join(environments)}), so it may deploy"
    if "workflow_call" not in triggers:
        return "does not declare workflow_call"
    config = triggers["workflow_call"] if isinstance(triggers["workflow_call"], Mapping) else {}
    for kind in ("inputs", "secrets"):
        declared = config.get(kind) if isinstance(config.get(kind), Mapping) else {}
        required = sorted(
            str(name)
            for name, spec in declared.items()
            if isinstance(spec, Mapping) and spec.get("required") and "default" not in spec
        )
        if required:
            return f"workflow_call requires {kind} the audit does not pass: {', '.join(required)}"
    problem = _permissions_problem(document.get("permissions"))
    if problem:
        return f"{problem}; the audit's token holds only contents: read and checks: read"
    for job_id, job in jobs.items():
        problem = _permissions_problem(job.get("permissions"))
        if problem:
            return (
                f"job {job_id} {problem}; the audit's token holds only contents: read and "
                "checks: read"
            )
    return None


def analyze_workflow(path: str, text: str) -> WorkflowReport:
    """Read one workflow file: its triggers and the check runs each job produces."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return _empty_report(path, f"not valid YAML: {exc}")
    if not isinstance(document, Mapping):
        return _empty_report(path, "not a workflow mapping")
    triggers = _triggers(document)
    notes: list[str] = []
    covered, problem = _push_covers_train_refs(triggers)
    if problem:
        notes.append(problem)
    raw_jobs = {
        str(job_id): job
        for job_id, job in (
            document.get("jobs") if isinstance(document.get("jobs"), Mapping) else {}
        ).items()
        if isinstance(job, Mapping)
    }
    decided = {job_id: job_runs_on_push(job.get("if")) for job_id, job in raw_jobs.items()}
    deploy_jobs = {job_id for job_id, job in raw_jobs.items() if job.get("environment") is not None}
    # A deploy job whose ``if:`` is provably false on a train ref (a main-only
    # deploy) leaves the rest of the workflow ordinary CI.  One that might run
    # makes the whole workflow a deployment: the train's refs must never be
    # added to its trigger, and where they already are, every candidate deploys.
    risky = sorted(job_id for job_id in deploy_jobs if decided[job_id] is not False)
    deployment = bool(risky)
    if deployment and covered:
        notes.append(
            "deployment workflow runs on the train's refs: "
            f"{', '.join(risky)} deploys on every candidate push"
        )
    pull_request = "pull_request" in triggers or "pull_request_target" in triggers
    push = "push" in triggers
    # A workflow gates the train when it runs on the train's refs, or could once
    # its push trigger names them.  A workflow_run follow-up or a schedule never
    # does, and neither does a deployment.
    # The main push audit verifies promotions after the fact; it gates nothing.
    audit = AUDIT_VARIABLE_REFERENCE in text
    gate = (covered or ((pull_request or push) and not deployment)) and not audit
    statuses: dict[str, JobChecks] = {}
    for job_id, job in raw_jobs.items():
        names, unresolved = job_check_names(job_id, job)
        if deployment:
            statuses[job_id] = JobChecks(job_id, names, "excluded", "deployment workflow")
        elif job_id in deploy_jobs:
            statuses[job_id] = JobChecks(
                job_id, names, "excluded", "deploy job never runs on the train's refs"
            )
        elif audit:
            statuses[job_id] = JobChecks(job_id, names, "excluded", "the main push audit")
        elif not gate:
            statuses[job_id] = JobChecks(
                job_id, names, "excluded", "workflow never runs on the train's refs"
            )
        elif unresolved:
            statuses[job_id] = JobChecks(job_id, names, "unresolved", unresolved)
        elif decided[job_id] is False:
            statuses[job_id] = JobChecks(job_id, names, "excluded", "job if: skips push events")
        elif decided[job_id] is None:
            statuses[job_id] = JobChecks(
                job_id,
                names,
                "required",
                "job if: could not be decided for push; confirm it runs on the train's refs",
            )
        else:
            statuses[job_id] = JobChecks(job_id, names, "required")
    # GitHub skips a job whose need was skipped unless its own ``if:`` asks to
    # run anyway, and the CI observer counts a skipped required check as red.
    skipped = {job_id for job_id, job in statuses.items() if job.status == "excluded"}
    changed = True
    while changed and not deployment:
        changed = False
        for job_id, job in raw_jobs.items():
            status = statuses[job_id]
            if status.status == "excluded":
                continue
            missing = [need for need in _needs(job) if need in skipped]
            condition = str(job.get("if") or "")
            if missing and not any(marker in condition for marker in _RUNS_AFTER_SKIPPED_NEED):
                statuses[job_id] = JobChecks(
                    job_id,
                    status.names,
                    "excluded",
                    f"needs {', '.join(missing)}, which does not run on the train's refs",
                )
                skipped.add(job_id)
                changed = True
    for job_id, job in raw_jobs.items():
        runs_on = job.get("runs-on")
        labels = runs_on if isinstance(runs_on, list) else [runs_on]
        if statuses[job_id].status != "excluded" and "self-hosted" in labels:
            notes.append(f"job {job_id} runs on a self-hosted runner, which must be online")
    return WorkflowReport(
        path=path,
        name=str(document.get("name") or path),
        push_on_train_refs=covered,
        pull_request=pull_request,
        push=push,
        deployment=deployment,
        jobs=tuple(statuses[job_id] for job_id in raw_jobs),
        notes=tuple(notes),
        workflow_call="workflow_call" in triggers,
        audit=audit,
        call_refusal=_call_refusal(triggers, document, raw_jobs),
    )


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


def github_full_name(repository_url: str | None) -> str | None:
    """``OWNER/REPO`` for a github.com URL, else ``None``."""
    from src.projects.github import GitHubError, parse_github_repository

    if not repository_url or not repository_url.strip() or _is_local(repository_url):
        return None
    try:
        return parse_github_repository(repository_url).full_name
    except GitHubError:
        return None


def _is_local(repository_url: str | None) -> bool:
    from src.integration.delivery_path import local_repository_path

    return local_repository_path(repository_url) is not None


def check_set_version(names: Sequence[str]) -> str:
    """A version that changes exactly when the required names change."""
    digest = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()
    return f"ci-{digest[:12]}"


@dataclass(frozen=True)
class CheckSet:
    names: tuple[str, ...]
    #: Workflows the names come from.
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    shape: str
    full_name: str | None
    required: CheckSet
    #: Workflows whose push trigger must gain the train's refs.
    trigger_fixes: tuple[str, ...]
    workflows: tuple[WorkflowReport, ...]
    problems: tuple[str, ...]


def _check_set(reports: Sequence[WorkflowReport]) -> tuple[CheckSet, list[str]]:
    """The union of required names, and a problem for each name two jobs share.

    The CI observer takes the newest check run of a required name across every
    push suite, so a shared name lets one job's success hide the other's
    failure.  The planner refuses to bind such a set rather than merge it.
    """
    producers: dict[str, list[str]] = {}
    for report in reports:
        for job in report.jobs:
            if job.status == "required":
                for name in job.names:
                    producers.setdefault(name, []).append(f"{report.path}:{job.job_id}")
    problems = [
        f"check name {name!r} is produced by {', '.join(jobs)}; rename one: the observer "
        "reads only the newest run of a name, so one job could hide the other's failure"
        for name, jobs in producers.items()
        if len(jobs) > 1
    ]
    return CheckSet(tuple(producers), tuple(report.path for report in reports)), problems


def classify(repository_url: str | None, workflows: Mapping[str, str]) -> Classification:
    """Decide the project's train shape from its repository URL and workflow files."""
    reports = tuple(analyze_workflow(path, text) for path, text in sorted(workflows.items()))
    full_name = github_full_name(repository_url)
    # Unresolved and undecided jobs matter only where the check set comes from.
    problems = [
        f"{report.path}: {job.job_id}: {job.reason}"
        for report in reports
        if report.push_on_train_refs or report.is_ci_candidate
        for job in report.jobs
        if job.status == "unresolved" or (job.status == "required" and job.reason)
    ]
    if full_name is None:
        shape = (
            "local_remote"
            if _is_local(repository_url) or not repository_url
            else "unsupported_forge"
        )
        return Classification(shape, None, CheckSet((), ()), (), reports, tuple(problems))
    canonical = f"https://github.com/{full_name}.git"
    if repository_url.strip() != canonical:
        problems.append(
            f"repository URL {repository_url!r} is not the form integration binding accepts "
            f"({canonical}); correct the project's repository URL first"
        )

    live = [report for report in reports if report.push_on_train_refs and report.required_names]
    if live:
        required, duplicates = _check_set(live)
        # CI beside the live workflows cannot gate the train until its push
        # trigger names the train's refs; say so, but the shape holds.
        advisory = tuple(
            report.path
            for report in reports
            if report.pull_request
            and report.is_ci_candidate
            and not report.push_on_train_refs
            and report.required_names
        )
        return Classification(
            "github_ci", full_name, required, advisory, reports, tuple(problems + duplicates)
        )
    pending = [
        report
        for report in reports
        if report.is_ci_candidate and not report.push_on_train_refs and report.required_names
    ]
    # Pull-request CI is the project's CI when it has any; only a project whose
    # CI runs on push alone falls back to its push workflows.
    candidates = [report for report in pending if report.pull_request] or pending
    if candidates:
        required, duplicates = _check_set(candidates)
        return Classification(
            "github_ci_trigger_missing",
            full_name,
            required,
            tuple(r.path for r in candidates),
            reports,
            tuple(problems + duplicates),
        )
    return Classification("github_no_ci", full_name, CheckSet((), ()), (), reports, tuple(problems))


# ---------------------------------------------------------------------------
# Policy, trust manifest and CI template
# ---------------------------------------------------------------------------


def bundle_route(bundle_dir: Path) -> PlaybookRoute:
    """The frozen route identity of one reviewed bundle, exactly as an import stores it."""
    from src.playbooks.definition import artifact_sha256, load_definition_json

    definition = load_definition_json((bundle_dir / "artifact.json").read_text(encoding="utf-8"))
    sha = artifact_sha256(definition)
    recorded = (bundle_dir / "artifact.sha256").read_text(encoding="utf-8").strip()
    if recorded != sha:
        raise ValueError(f"{bundle_dir.name}: artifact.sha256 does not match artifact.json")
    scope = definition.scope.type
    identifier = definition.scope.project_id if scope == "project" else ""
    return PlaybookRoute.model_validate(
        {
            "playbook_id": definition.id,
            "scope": scope,
            "scope_identifier": identifier or "",
            "activation_id": None,
            "artifact": {
                "playbook_id": definition.id,
                "artifact_sha256": sha,
                "schema_generation": definition.schema_version,
                "contract_fingerprint": definition.contract_fingerprint(),
                "source_digest": definition.source_hash,
                "compiler_build": definition.compiler_build,
                # ArtifactStore.put records a nullable compile time; preflight
                # compares the route with that stored reference.
                "compiled_at": None,
                "version": definition.version,
            },
        }
    )


def select_routes(
    bundle_root: Path, project_id: str, route: str = "auto"
) -> tuple[PlaybookRoute, PlaybookRoute]:
    """``(parent, root)`` routes: the project's own reviewed pair, or the shared pair."""
    own = (
        bundle_root / f"{project_id}-parent-integration",
        bundle_root / f"{project_id}-root-train",
    )
    shared = (bundle_root / SHARED_PARENT_ROUTE, bundle_root / SHARED_ROOT_ROUTE)
    if route == "project" or (route == "auto" and all(path.is_dir() for path in own)):
        chosen = own
    elif route in ("shared", "auto"):
        chosen = shared
    else:
        raise ValueError("route must be auto, shared or project")
    missing = [path.name for path in chosen if not path.is_dir()]
    if missing:
        raise ValueError(f"no reviewed bundle for {', '.join(missing)}")
    return bundle_route(chosen[0]), bundle_route(chosen[1])


def producer_for(credential_mode: str) -> str:
    """The policy producer: GitHub Actions' numeric App id in every credential mode."""
    if credential_mode not in CREDENTIAL_MODES:
        raise ValueError(f"credential mode must be one of {', '.join(CREDENTIAL_MODES)}")
    return str(GITHUB_ACTIONS_APP_ID)


def build_policy(
    *,
    checks: Sequence[str],
    check_version: str,
    producer_id: str,
    parent_route: PlaybookRoute,
    root_route: PlaybookRoute,
    intelligence_class: str,
    profile_id: str,
) -> dict[str, Any]:
    """The project's hierarchical integration policy, validated by its model."""
    if not checks:
        raise ValueError("a train policy needs at least one required check")

    def boundary(route: PlaybookRoute) -> dict[str, Any]:
        return {
            "required_checks": {
                "version": check_version,
                "names": list(checks),
                "producer_id": producer_id,
            },
            "repair": {
                "debug_intelligence_class": intelligence_class,
                "debug_profile_id": profile_id,
            },
            "route": route.model_dump(mode="json"),
            "primary_intelligence_class": intelligence_class,
            "primary_profile_id": profile_id,
            "verifier_intelligence_class": intelligence_class,
            "verifier_profile_id": profile_id,
        }

    policy = HierarchicalIntegrationPolicy.model_validate(
        {
            "version": 1,
            "parent": boundary(parent_route),
            "root": boundary(root_route),
            "branchless_parent": "verifier",
            "on_failed_child": "block",
            "on_main_moved": "rebuild",
        }
    )
    return policy.model_dump(mode="json")


def build_trust_manifest(
    *,
    canonical_repository_id: str,
    github_repository_id: int,
    full_name: str,
    attestation_app_id: int,
    checks: Sequence[str],
    check_version: str,
) -> dict[str, Any]:
    """``.github/agent-queue-integration.json`` for App credential mode.

    A thin wrapper over the shared builder (``src/integration/trust_manifest.py``)
    that ``aq integration trust-manifest`` and the preflight use too; the planner's
    producer is always GitHub Actions.
    """
    from src.integration import trust_manifest

    return trust_manifest.build_trust_manifest(
        canonical_repository_id=canonical_repository_id,
        repository_id=github_repository_id,
        full_name=full_name,
        ci_producer_app_id=GITHUB_ACTIONS_APP_ID,
        attestation_app_id=attestation_app_id,
        checks=checks,
        check_version=check_version,
    )


def detect_stack(files: Mapping[str, str]) -> str:
    """``python``, ``pnpm``, ``npm`` or ``unknown`` from top-level file names."""
    if "package.json" in files:
        return "pnpm" if "pnpm-lock.yaml" in files else "npm"
    if "pyproject.toml" in files or "requirements.txt" in files:
        return "python"
    return "unknown"


def _package_scripts(files: Mapping[str, str]) -> dict[str, str]:
    try:
        scripts = json.loads(files.get("package.json") or "{}").get("scripts") or {}
    except (ValueError, AttributeError):
        return {}
    return scripts if isinstance(scripts, dict) else {}


def _python_install(files: Mapping[str, str]) -> str:
    pyproject = files.get("pyproject.toml") or ""
    if pyproject:
        extras = re.search(
            r"^\[project\.optional-dependencies\](.*?)(?:^\[|\Z)",
            pyproject,
            re.MULTILINE | re.DOTALL,
        )
        dev = extras is not None and re.search(r"^dev\s*=", extras.group(1), re.MULTILINE)
        return "python -m pip install -e '.[dev]'" if dev else "python -m pip install -e . pytest"
    return "python -m pip install -r requirements.txt pytest"


def suggested_commands(files: Mapping[str, str]) -> tuple[str, ...]:
    """Install-and-test commands for the detected stack, in order.  Empty when unknown."""
    stack = detect_stack(files)
    scripts = _package_scripts(files)
    if stack == "pnpm":
        if "check" in scripts:
            return ("pnpm install --frozen-lockfile", "pnpm check")
        return ("pnpm install --frozen-lockfile",) + tuple(
            f"pnpm {script}" for script in ("test", "build") if script in scripts
        )
    if stack == "npm":
        install = "npm ci" if "package-lock.json" in files else "npm install"
        return (install,) + tuple(
            "npm test" if script == "test" else f"npm run {script}"
            for script in ("test", "build")
            if script in scripts
        )
    if stack == "python":
        return (_python_install(files), "python -m pytest -q")
    return ()


def ci_workflow_template(files: Mapping[str, str], *, test_command: str | None = None) -> str:
    """A starting ``.github/workflows/ci.yml`` whose single ``Tests`` job gates the train."""
    stack = detect_stack(files)
    steps = ["      - uses: actions/checkout@v4"]
    if stack == "python":
        steps += [
            "      - uses: actions/setup-python@v5",
            "        with:",
            "          python-version: '3.12'",
        ]
    elif stack in ("npm", "pnpm"):
        if stack == "pnpm":
            steps.append("      - uses: pnpm/action-setup@v4")
        version = "node-version-file: .nvmrc" if ".nvmrc" in files else "node-version: '20'"
        steps += ["      - uses: actions/setup-node@v4", "        with:", f"          {version}"]
    commands = list(suggested_commands(files))
    if test_command:
        commands = [test_command]
    if not commands:
        commands = ["echo 'Replace this step with the project test command' && exit 1"]
    for command in commands:
        steps.append(f"      - run: {command}")
    body = "\n".join(steps)
    return f"""name: CI

# Written by `aq integration onboard-train` (docs/config/train-onboarding.md).
# The integration train accepts only check runs from a push of the exact
# candidate it built, so the push trigger names the train's refs:
# aq/integration/** (root candidates) and aq/parent/** (parent snapshots);
# main shows that the default branch is green. workflow_call lets the App-mode
# main push audit (main-attestation.yml) re-run this CI for a push to main
# that carries no integration attestation.
# Keep `Tests` as the job name: it is the required check in the project's
# train policy. Never add deploy steps or secrets to this workflow.
on:
  pull_request:
  push:
    branches:
      - main
      - 'aq/integration/**'
      - 'aq/parent/**'
  workflow_dispatch:
  workflow_call:

permissions:
  contents: read

jobs:
  tests:
    name: Tests
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
{body}
"""


def trigger_fix(report: WorkflowReport, text: str) -> str:
    """The push trigger the workflow needs, keeping what it runs on today.

    ``branches`` gains the train's patterns.  A ``branches-ignore`` filter
    (which GitHub forbids beside ``branches``) must instead stop matching them,
    and a ``paths`` filter must go, or a candidate could still produce no runs.
    """
    triggers = _triggers(yaml.safe_load(text) or {})
    push = triggers.get("push")
    config = push if isinstance(push, Mapping) else {}
    comments = [f"# {report.path}"]
    for key in ("paths", "paths-ignore"):
        if config.get(key):
            comments.append(f"# remove push.{key}: a path filter can leave a candidate untested")
    ignored = config.get("branches-ignore")
    if ignored is not None:
        ignored = [ignored] if isinstance(ignored, str) else [str(item) for item in ignored]
        blocking = [
            pattern
            for pattern in ignored
            if any(_filter_regex(pattern).match(ref) for ref in TRAIN_REF_SAMPLES)
        ]
        comments.append(
            "# push uses branches-ignore; drop the patterns that match the train's refs: "
            + (", ".join(blocking) if blocking else "(none match; check the other filters)")
        )
        return "\n".join(comments)
    branches = config.get("branches")
    current = [branches] if isinstance(branches, str) else [str(b) for b in branches or ()]
    wanted = list(dict.fromkeys([*current, *TRAIN_BRANCH_PATTERNS]))
    lines = "\n".join(f"      - '{pattern}'" for pattern in wanted)
    return "\n".join(comments) + f"\non:\n  push:\n    branches:\n{lines}"


# ---------------------------------------------------------------------------
# The main push audit (App-mode integration train spec §7.2, §11)
# ---------------------------------------------------------------------------

#: The heredoc delimiter the audit workflow writes the verifier with.
VERIFIER_DELIMITER = "AQ_HOSTED_ATTESTATION_PY"
_VERIFIER_FILE = '"$RUNNER_TEMP/aq_hosted_attestation.py"'
_JOB_ID_UNSAFE = re.compile(r"[^a-z0-9_-]+")
#: What the template interpolates into YAML and shell text unquoted.
_SAFE_REF = re.compile(r"[A-Za-z0-9._/-]+")
_SAFE_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9._-]+\.ya?ml")


@dataclass(frozen=True)
class AuditFallback:
    """What ``main-attestation.yml`` runs for a push to the default branch it cannot trust."""

    #: Gating CI workflows it re-runs through ``workflow_call``.
    calls: tuple[str, ...]
    #: ``(path, reason)`` for each gating workflow it must not call.
    uncalled: tuple[tuple[str, str], ...]

    @property
    def fails(self) -> bool:
        """Whether a failing "unattested push" job stands in for CI it cannot re-run."""
        return bool(self.uncalled) or not self.calls

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": list(self.calls),
            "uncalled": [{"path": path, "reason": reason} for path, reason in self.uncalled],
            "fails": self.fails,
        }


def audit_fallback(classification: Classification) -> AuditFallback:
    """The audit's fallback: the project's gating CI workflows that can be called safely."""
    reports = {report.path: report for report in classification.workflows}
    calls: list[str] = []
    uncalled: list[tuple[str, str]] = []
    for path in classification.required.sources:
        refusal = reports[path].call_refusal
        if refusal is None and _SAFE_PATH.fullmatch(path) is None:
            refusal = "is not a plain .github/workflows/*.yml path, which workflow_call needs"
        if refusal is None:
            calls.append(path)
        else:
            uncalled.append((path, refusal))
    return AuditFallback(tuple(calls), tuple(uncalled))


def hosted_verifier_source() -> str:
    """``src/integration/hosted_attestation.py``, the text the audit embeds."""
    return HOSTED_VERIFIER_PATH.read_text(encoding="utf-8")


def _embeddable(source: str) -> str:
    """``source`` as the body of a YAML literal block inside a quoted heredoc, unchanged.

    GitHub substitutes ``${{ }}`` in a ``run`` script before the shell sees it,
    and a literal block keeps neither trailing blanks nor carriage returns, so
    any of those would change the embedded copy; the delimiter would end it.
    """
    lines = source.split("\n")
    if not source.endswith("\n") or "${{" in source or "\r" in source:
        raise ValueError("the verifier must end with a newline and hold no ${{ or carriage return")
    if any(line != line.rstrip() or line.strip() == VERIFIER_DELIMITER for line in lines):
        raise ValueError("the verifier has trailing whitespace or the heredoc delimiter")
    return "\n".join(f"          {line}" if line else "" for line in lines[:-1])


def _call_job_ids(paths: Sequence[str]) -> list[str]:
    if len(paths) == 1:
        return ["unattested-ci"]
    ids: list[str] = []
    for path in paths:
        stem = _JOB_ID_UNSAFE.sub("-", Path(path).stem.lower()).strip("-") or "ci"
        candidate = f"unattested-ci-{stem}"
        while candidate in ids:
            candidate += "-x"
        ids.append(candidate)
    return ids


_UNATTESTED = (
    "    needs: attestation\n"
    "    if: >-\n"
    "      needs.attestation.outputs.configured == 'true' &&\n"
    "      needs.attestation.outputs.attested != 'true'\n"
)


def audit_workflow_template(
    fallback: AuditFallback, *, default_branch: str = "main", verifier: str | None = None
) -> str:
    """``.github/workflows/main-attestation.yml`` for one project.

    The same audit as agent-queue's own workflow, made self-contained: the
    verifier is embedded verbatim from :func:`hosted_verifier_source` instead of
    read from a sparse checkout, and the job runs on ``ubuntu-latest`` whatever
    runners the project's CI uses.  An unattested push re-runs each workflow in
    ``fallback.calls``; a gating workflow it must not call (see
    :func:`_call_refusal`) is replaced by a job that fails with an "unattested
    push" summary, so the push stays visible.  The workflow holds no secret and
    deploys nothing.
    """
    if _SAFE_REF.fullmatch(default_branch) is None:
        raise ValueError(f"default branch {default_branch!r} is not a plain branch name")
    for path in fallback.calls:
        if _SAFE_PATH.fullmatch(path) is None:
            raise ValueError(f"the audit cannot call {path!r}")
    body = _embeddable(hosted_verifier_source() if verifier is None else verifier)
    branch = json.dumps(default_branch)
    if fallback.calls and not fallback.uncalled:
        fallback_text = "re-runs the project's CI (" + ", ".join(fallback.calls) + ")"
    elif fallback.calls:
        fallback_text = (
            "re-runs the CI it can call and fails an `Unattested push` job for the rest"
        )
    else:
        fallback_text = "fails an `Unattested push` job, because no gating CI can be called"
    jobs: list[str] = []
    for path, job_id in zip(fallback.calls, _call_job_ids(fallback.calls), strict=True):
        jobs.append(f"  {job_id}:\n{_UNATTESTED}    uses: ./{path}\n")
    if fallback.fails:
        why = (
            "; ".join(f"{path} {reason}" for path, reason in fallback.uncalled)
            or "the project has no gating CI workflow"
        )
        explanation = json.dumps(
            f"No workflow re-runs CI for this push ({why}). Run the project's CI on this "
            "commit by hand and review it."
        )
        if "${{" in explanation:
            raise ValueError("a workflow path holds ${{")
        jobs.append(
            "  unattested-push:\n"
            f"    name: Unattested push to {default_branch}\n"
            f"{_UNATTESTED}"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - env:\n"
            "          REASON: ${{ needs.attestation.outputs.reason }}\n"
            f"          UNCALLED: {explanation}\n"
            "        run: |\n"
            "          {\n"
            f'            echo "### Unattested push to {default_branch}"\n'
            '            echo ""\n'
            '            echo "No valid Agent Queue integration attestation: $REASON"\n'
            '            echo ""\n'
            '            echo "$UNCALLED"\n'
            '          } >> "$GITHUB_STEP_SUMMARY"\n'
            f'          echo "::error title=Unattested push to {default_branch}::$REASON"\n'
            "          exit 1\n"
        )
    return f"""name: Main attestation
# Written by `aq integration onboard-train --write-audit-workflow`
# (docs/config/app-mode-train.md). The break-glass audit (App-mode integration
# train spec §7.2): a train promotion moves {default_branch} to a candidate the
# daemon already attested, so this workflow only verifies that attestation. A
# push without a valid attestation {fallback_text}.
# With the Actions variables AQ_INTEGRATION_ATTESTATION_APP_ID and
# AQ_INTEGRATION_REQUIRED_CHECK_VERSION unset the repository is not in App
# mode yet, and nothing runs. The verify step embeds agent-queue's
# src/integration/hosted_attestation.py verbatim: regenerate this file, never
# edit it. It holds no secret and deploys nothing.
on:
  push:
    branches: [{branch}]
permissions:
  contents: read
  checks: read
jobs:
  attestation:
    name: Main attestation
    runs-on: ubuntu-latest
    outputs:
      attested: ${{{{ steps.verify.outputs.attested }}}}
      configured: ${{{{ steps.verify.outputs.configured }}}}
      reason: ${{{{ steps.verify.outputs.reason }}}}
    steps:
      - id: verify
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          REPOSITORY: ${{{{ github.repository }}}}
          REPOSITORY_ID: ${{{{ github.repository_id }}}}
          SHA: ${{{{ github.sha }}}}
          APP_ID: ${{{{ vars.AQ_INTEGRATION_ATTESTATION_APP_ID }}}}
          CHECK_VERSION: ${{{{ vars.AQ_INTEGRATION_REQUIRED_CHECK_VERSION }}}}
        run: |
          cat > {_VERIFIER_FILE} <<'{VERIFIER_DELIMITER}'
{body}
          {VERIFIER_DELIMITER}
          python3 {_VERIFIER_FILE}
{"".join(jobs)}"""


def embedded_verifier(workflow_text: str) -> str:
    """The verifier a rendered audit workflow embeds, as the runner would write it."""
    document = yaml.safe_load(workflow_text)
    steps = document["jobs"]["attestation"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "verify")
    head, _, rest = script.partition("\n")
    if not head.endswith(f"<<'{VERIFIER_DELIMITER}'"):
        raise ValueError("the verify step does not open the verifier heredoc")
    source, found, _ = rest.partition(f"\n{VERIFIER_DELIMITER}\n")
    if not found:
        raise ValueError("the verify step does not close the verifier heredoc")
    return source + "\n"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectFacts:
    project_id: str
    repository_url: str
    default_branch: str = "main"
    integration_repository_id: str | None = None
    #: ``projects.hierarchical_integration_mode``; ``None`` when unread.
    current_mode: str | None = None
    #: ``(task_id, pr_url)`` of completed tasks whose pull request is legacy.
    legacy_pull_requests: tuple[tuple[str, str], ...] = ()
    #: BLOCKED tasks a local-remote project must deliver before development mode.
    blocked_tasks: tuple[str, ...] = ()
    #: Legacy pull requests whose state could not be checked.
    unchecked_pull_requests: int = 0


@dataclass(frozen=True)
class Step:
    title: str
    commands: tuple[str, ...] = ()
    note: str | None = None


@dataclass
class OnboardingPlan:
    project_id: str
    shape: str
    path: str
    required_checks: tuple[str, ...]
    check_version: str | None
    credential_mode: str
    policy: dict[str, Any] | None
    trust_manifest: dict[str, Any] | None
    validation_commands: tuple[str, ...]
    workflows: tuple[WorkflowReport, ...]
    problems: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    #: App mode: the default branch's target ruleset (``--write-ruleset``).
    ruleset: dict[str, Any] | None = None
    #: GitHub projects: what the ``main`` push audit runs for an unattested push
    #: (:meth:`AuditFallback.as_dict` and the workflow's path).
    audit_workflow: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["workflows"] = [
            {**asdict(report), "required_names": list(report.required_names)}
            for report in self.workflows
        ]
        return data


GENERATION_HELPER = (
    "generation() {\n"
    "  aq --json integration status {project} |\n"
    '    python3 -c \'import json,sys; print(json.load(sys.stdin)["data"]["generation"])\'\n'
    "}"
)
READY_CHECK = (
    "aq --json integration status {project} | python3 -c 'import json,sys; "
    'd=json.load(sys.stdin)["data"]; b=d.get("blockers") or []; '
    'print("ready" if d.get("ready") and not b else "NOT READY"); '
    '[print(" ", x.get("code"), x.get("ref"), x.get("cause") or "") for x in b]; '
    'sys.exit(0 if d.get("ready") and not b else 1)\''
)


def _q(value: str) -> str:
    return shlex.quote(value)


@dataclass(frozen=True)
class _AppAnchors:
    """App credential mode's repository-side trust anchors, as plan steps (spec §11).

    The daemon's App-mode commands resolve the project's repository record, so
    the steps run them with ``--repository-id`` when one is designated.  Before
    the bind creates the record there is nothing to resolve: the steps then use
    the local equivalents this run writes (``--write-trust-manifest``,
    ``--write-ruleset``) and ``gh variable set``, and ``app-verify`` confirms
    everything after the bind.
    """

    facts: ProjectFacts
    full_name: str
    app_id: int | None
    check_version: str | None
    policy_path: str
    manifest_path: str
    audit_workflow_path: str
    ruleset_path: str
    fallback: AuditFallback

    def _aq(self, verb: str, *extra: str) -> str:
        parts = ["aq", "integration", verb, self.facts.project_id, "--policy", self.policy_path]
        if self.facts.integration_repository_id:
            parts += ["--repository-id", self.facts.integration_repository_id]
        return shlex.join([*parts, *extra])

    def _audit_note(self) -> str:
        calls, uncalled = self.fallback.calls, self.fallback.uncalled
        text = (
            f"For a push to {self.facts.default_branch} without a valid attestation the audit "
        )
        if calls:
            text += f"re-runs {', '.join(calls)} through workflow_call."
        else:
            text += "fails an `Unattested push` job: it has no gating CI workflow to call."
        if uncalled:
            text += (
                " It does not call "
                + "; ".join(f"{path} ({reason})" for path, reason in uncalled)
                + ", and fails an `Unattested push` job in its place. A gating workflow that "
                "declares workflow_call, and has no environment, required inputs or secrets, "
                "and no permission beyond contents: read and checks: read, is re-run instead."
            )
        return text

    def land_step(self) -> Step:
        branch = self.facts.default_branch
        audit = (
            AUDIT_WORKFLOW_PATH
            if self.audit_workflow_path == AUDIT_WORKFLOW_PATH
            else f"{self.audit_workflow_path} as {AUDIT_WORKFLOW_PATH}"
        )
        if self.facts.integration_repository_id:
            manifest = f"{TRUST_MANIFEST_PATH} (trust-manifest --write)"
            commands: tuple[str, ...] = (
                self._aq("trust-manifest", "--write", TRUST_MANIFEST_PATH),
            )
            source = (
                "The daemon renders the manifest from what it trusts: the authenticated "
                "binding, its own App and the policy."
            )
        else:
            manifest = (
                TRUST_MANIFEST_PATH
                if self.manifest_path == TRUST_MANIFEST_PATH
                else f"{self.manifest_path} as {TRUST_MANIFEST_PATH}"
            ) + " (--write-trust-manifest)"
            commands = ()
            source = (
                "The daemon's trust-manifest command resolves the repository record, which "
                "the bind step creates, so --write-trust-manifest writes the same bytes now."
            )
        commands += (
            (
                f"# commit {manifest} and {audit} "
                f"(--write-audit-workflow) to {branch}, through the project's current delivery "
                "path"
            ),
        )
        return Step(
            "App mode: land the trust manifest and the audit workflow",
            commands,
            "Both files are inert until the Actions variables are set: with them unset the "
            "audit reports configured=false and runs nothing. So they land before the drain, "
            "through the project's current path (disabled mode: a pull request a human "
            f"merges; development mode: the development publisher). {source} "
            + self._audit_note(),
        )

    def variables_step(self) -> Step:
        app_id = str(self.app_id or "APP_ID")
        version = self.check_version or "CHECK_VERSION"
        if self.facts.integration_repository_id:
            commands: tuple[str, ...] = (self._aq("app-setup", "--apply"),)
            how = (
                "app-setup --apply runs gh variable set only for a variable that differs, then "
                "re-verifies through the App: its variables item must be ok."
            )
        else:
            commands = tuple(
                f"gh variable set {name} --repo {self.full_name} --body {_q(value)}"
                for name, value in (
                    ("AQ_INTEGRATION_ATTESTATION_APP_ID", app_id),
                    ("AQ_INTEGRATION_REQUIRED_CHECK_VERSION", version),
                )
            )
            how = (
                "Before the bind creates the repository record the daemon's app-setup cannot "
                "resolve it, so set them with gh directly; app-verify confirms them after the bind."
            )
        return Step(
            "App mode: set the Actions variables (repository admin)",
            commands,
            f"AQ_INTEGRATION_ATTESTATION_APP_ID={app_id} (the daemon's App) and "
            f"AQ_INTEGRATION_REQUIRED_CHECK_VERSION={version} (the policy's root check-set "
            "version), set with your own gh login, which must be a repository admin: the "
            "daemon's App reads them and never writes them. They switch the audit on, so set "
            f"them once the drain has stopped every unattested push to "
            f"{self.facts.default_branch}. {how}",
        )

    def ruleset_step(self) -> Step:
        from src.integration.app_mode import target_ruleset_name

        branch = self.facts.default_branch
        name = target_ruleset_name(branch)
        ruleset = _q(self.ruleset_path)
        post = f"gh api --method POST repos/{self.full_name}/rulesets --input {ruleset}"
        put = f"gh api --method PUT repos/{self.full_name}/rulesets/RULESET_ID --input {ruleset}"
        if self.facts.integration_repository_id:
            commands: tuple[str, ...] = (
                self._aq("app-setup"),
                post,
                f"# or, to replace the ruleset app-setup names: {put}",
                self._aq("app-verify"),
            )
            verify = "app-verify must then report protection attested_only."
        else:
            commands = (
                f"gh api repos/{self.full_name}/rulesets --jq '.[] | [.id, .name] | @tsv'",
                post,
                f"# or, to replace an existing ruleset named {name}: {put}",
            )
            verify = "After the bind, app-verify must report protection attested_only."
        return Step(
            f"App mode: require the attestation on {branch} (repository admin)",
            commands,
            f"{self.ruleset_path} is written by --write-ruleset: ruleset {name!r} requires "
            f"{ATTESTATION_NAME!r} pinned to App {self.app_id or 'APP_ID'} and has no bypass "
            f"actor. A promotion fast-forwards {branch} to a candidate the daemon already "
            "attested, so it satisfies the rule without bypassing it, and GitHub refuses "
            "every unattested push, the daemon's included. Remove any other rule the App "
            "cannot satisfy (a pull-request, signature or unpinned status-check rule, or "
            "classic branch protection): app-verify reports it as "
            f"branch_protection_incompatible. {verify} Leaving the train for development "
            "needs the App's bypass back first (runbook §9.6).",
        )


def plan_onboarding(
    facts: ProjectFacts,
    classification: Classification,
    *,
    parent_route: PlaybookRoute | None,
    root_route: PlaybookRoute | None,
    credential_mode: str = "existing-login",
    intelligence_class: str = "standard-high",
    profile_id: str = "standard-high-codex",
    check_names: Sequence[str] | None = None,
    check_version: str | None = None,
    attestation_app_id: int | None = None,
    github_repository_id: int | None = None,
    validation: Sequence[str] = (),
    interval_seconds: int = 300,
    policy_path: str = "train-policy.json",
    manifest_path: str = TRUST_MANIFEST_PATH,
    workflow_path: str = ".github/workflows/ci.yml",
    audit_workflow_path: str = AUDIT_WORKFLOW_PATH,
    ruleset_path: str | None = None,
) -> OnboardingPlan:
    """Every step from the project's current state to a ready observe-mode train.

    A plan stops at the first prerequisite that changes the repository (a CI
    workflow, a trigger fix) or at a problem that makes the policy unsafe to
    bind: the rest depends on a re-run once that change is on the default
    branch.  In App credential mode the repository-side trust anchors come in
    the App-mode spec's §11 order: the manifest and audit workflow land through
    the project's current path, then the drain, the Actions variables, the
    ruleset, the bind and observe (``docs/config/app-mode-train.md``).
    """
    project = facts.project_id
    shape = classification.shape
    problems = list(classification.problems)
    if shape in ("local_remote", "unsupported_forge"):
        return _development_plan(facts, classification, problems, validation, interval_seconds)

    names = tuple(check_names) if check_names else classification.required.names
    version = check_version or (check_set_version(names) if names else None)
    conflicted = any("is produced by" in problem for problem in classification.problems)
    if check_names:
        conflicted = False
    policy = None
    manifest = None
    if names and not conflicted and parent_route is not None and root_route is not None:
        policy = build_policy(
            checks=names,
            check_version=version or check_set_version(names),
            producer_id=producer_for(credential_mode),
            parent_route=parent_route,
            root_route=root_route,
            intelligence_class=intelligence_class,
            profile_id=profile_id,
        )
    # Status reports the designated repository outside development mode; with no
    # designation the project's own id names the record the binding creates.
    # Unread status, or development mode (whose status omits it), is not a guess.
    repository_id = facts.integration_repository_id or (
        project if facts.current_mode not in (None, "development") else None
    )
    if repository_id is None:
        problems.append(
            "the designated integration repository id is unknown (integration status was "
            "unreadable, or the project is in development mode, whose status omits it): pass "
            "--repository-id with the project's existing repository id"
        )
    if credential_mode == "app" and names and classification.full_name:
        if attestation_app_id is None:
            problems.append("App credential mode needs the App id (integration.github_app.app_id)")
        elif github_repository_id is None:
            problems.append(
                "App credential mode needs the GitHub repository id: pass "
                f'--github-repository-id "$(gh api repos/{classification.full_name} --jq .id)"'
            )
        elif repository_id is not None:
            manifest = build_trust_manifest(
                canonical_repository_id=repository_id,
                github_repository_id=github_repository_id,
                full_name=classification.full_name,
                attestation_app_id=attestation_app_id,
                checks=names,
                check_version=version or check_set_version(names),
            )
    plan = OnboardingPlan(
        project_id=project,
        shape=shape,
        path="train",
        required_checks=names,
        check_version=version,
        credential_mode=credential_mode,
        policy=policy,
        trust_manifest=manifest,
        validation_commands=(),
        workflows=classification.workflows,
        problems=problems,
    )
    fallback = audit_fallback(classification)
    plan.audit_workflow = {"path": audit_workflow_path, **fallback.as_dict()}
    app = credential_mode == "app" and classification.full_name is not None
    if app and attestation_app_id is not None:
        from src.integration.app_mode import target_ruleset

        plan.ruleset = target_ruleset(attestation_app_id, facts.default_branch)
    anchors = _AppAnchors(
        facts=facts,
        full_name=classification.full_name or "",
        app_id=attestation_app_id,
        check_version=version,
        policy_path=policy_path,
        manifest_path=manifest_path,
        audit_workflow_path=audit_workflow_path,
        ruleset_path=ruleset_path or f"ruleset.{project}.json",
        fallback=fallback,
    )
    steps = plan.steps
    if shape == "github_no_ci":
        steps.append(
            Step(
                "Land a CI workflow first",
                (),
                "The train accepts only GitHub check runs from a push of the exact candidate, "
                f"and {project} has no workflow that could produce one. A local command cannot "
                f"stand in. Commit the template written to {workflow_path} (as "
                ".github/workflows/ci.yml) through the project's current delivery path "
                "(disabled mode: its pull request, merged by a human). Make its `Tests` job green "
                "on main, then run onboard-train again: the project then classifies as github_ci.",
            )
        )
        return plan
    if shape == "github_ci_trigger_missing":
        steps.append(
            Step(
                "Make CI run on the train's refs first",
                (),
                "The train reads CI only from push runs on aq/integration/** and aq/parent/**, "
                "and these workflows never push-trigger there: "
                + ", ".join(classification.trigger_fixes)
                + ". Land the trigger change printed under 'Trigger fix' through the project's "
                "current delivery path, then run onboard-train again. The observe preflight "
                "does not look at CI, so a train bound before this lands would stay red.",
            )
        )
    if facts.legacy_pull_requests or facts.unchecked_pull_requests:
        note = (
            "These completed tasks predate the train and are not enrolled in it: status "
            "ignores them and no batch will ever deliver them. Merge each approved, open PR "
            "now, while the project still uses its legacy review path (--method merge keeps "
            "the task's branch tip an ancestor of the default branch), or close it and refile "
            "the work as a new task after cutover. `aq git pr-merge` runs gh in the daemon's "
            "data directory, never in a project checkout."
        )
        if facts.unchecked_pull_requests:
            note += (
                f" {facts.unchecked_pull_requests} more PR(s) were not checked; list them with "
                f"`aq task list --project {project} --status COMPLETED` and check each."
            )
        steps.append(
            Step(
                "Resolve legacy pull requests before the drain",
                tuple(
                    command
                    for task_id, url in facts.legacy_pull_requests
                    for command in (
                        f"gh pr view {_q(url)} --json state,reviewDecision,mergeable  # {task_id}",
                        (
                            f"aq git pr-merge --project-id {_q(project)} --pr-url {_q(url)} "
                            "--method merge"
                        ),
                    )
                ),
                note,
            )
        )
    if app:
        steps.append(anchors.land_step())
    if shape == "github_ci_trigger_missing":
        return plan
    if policy is None or repository_id is None:
        steps.append(
            Step(
                "Resolve the problems above, then run onboard-train again",
                (),
                "No policy is bound while its check set is empty or ambiguous, its routes are "
                "missing, or the repository to bind is unknown.",
            )
        )
        return plan
    if parent_route is not None and root_route is not None:
        steps.append(_import_step(parent_route, root_route))
    steps.append(
        Step(
            "Read a fresh generation before every mutation",
            (
                GENERATION_HELPER.replace("{project}", _q(project)),
                f"aq integration status {_q(project)}",
            ),
        )
    )
    mode = facts.current_mode
    if mode is None:
        steps.append(
            Step(
                "Drain first unless status says disabled",
                (
                    (
                        f"aq integration enable {_q(project)} --mode disabled --expected-generation "
                        "\"$(generation)\" --reason 'drain for train cutover'"
                    ),
                ),
                "The project's mode could not be read. Run this only when status reports an "
                "effective mode other than disabled, then repeat status until draining is false.",
            )
        )
    elif mode != "disabled":
        steps.append(
            Step(
                f"Drain the {mode} publisher",
                (
                    (
                        f"aq integration enable {_q(project)} --mode disabled --expected-generation "
                        f"\"$(generation)\" --reason 'drain {mode} publisher for train cutover'"
                    ),
                    f"aq integration status {_q(project)}",
                ),
                "Repeat status until effective_mode is disabled and draining is false; the "
                "drain finishes once every frozen integration operation is terminal.",
            )
        )
    if app:
        steps.append(anchors.variables_step())
        steps.append(anchors.ruleset_step())
    if facts.integration_repository_id:
        bind = (
            f"aq project set {_q(project)} integration-repository-id {_q(repository_id)} "
            '--expected-integration-generation "$(generation)" '
            "--reason 'bind the existing repository for the train'"
        )
    else:
        record = json.dumps(
            {
                "id": repository_id,
                "url": facts.repository_url,
                "default_branch": facts.default_branch,
            },
            separators=(",", ":"),
        )
        bind = (
            f"aq project set {_q(project)} integration-repository {_q(record)} "
            '--expected-integration-generation "$(generation)" '
            "--reason 'bind the project repository for the train'"
        )
    policy_arg = (
        "\"$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1])), "
        f'separators=(",",":")))\' {_q(policy_path)})"'
    )
    steps.append(
        Step(
            "Bind repository, review mode and policy (disabled and drained)",
            (
                bind,
                (
                    f"aq project set {_q(project)} integration-review-mode pull_request "
                    '--expected-integration-generation "$(generation)" '
                    "--reason 'require pull request review'"
                ),
                (
                    f"aq project set {_q(project)} integration-policy {policy_arg} "
                    '--expected-integration-generation "$(generation)" '
                    "--reason 'bind the generated train policy'"
                ),
            ),
            f"The policy file is written by --write-policy {policy_path}.",
        )
    )
    steps.append(
        Step(
            "Observe: preflight without scheduling",
            (
                (
                    f"aq integration enable {_q(project)} --mode observe --expected-generation "
                    "\"$(generation)\" --reason 'preflight train configuration without scheduling'"
                ),
                f"aq integration flush {_q(project)}",
                f"aq integration adopt-legacy-deliveries --project-id {_q(project)} --dry-run",
                f"aq integration bind-legacy-repositories {_q(project)}",
                *((f"aq integration app-verify {_q(project)}",) if app else ()),
            ),
            "Adopt or bind only what the dry runs prove; both are safe to repeat."
            + (
                " app-verify reads the bound policy: every item must be ok (the preflight "
                "turns each fail into a blocker of the same name)."
                if app
                else ""
            ),
        )
    )
    steps.append(
        Step(
            "Ready check",
            (READY_CHECK.replace("{project}", _q(project)),),
            "Observe must report ready with zero blockers. The later hierarchy and train "
            "flips belong to the supervisor: `aq integration enable "
            f"{project} --mode hierarchy ...`, then `--mode train --interval-seconds 300`.",
        )
    )
    return plan


def _import_step(parent: PlaybookRoute, root: PlaybookRoute) -> Step:
    commands: list[str] = []
    for route in (parent, root):
        commands.append(
            f"aq playbook v2-validate --path reviewed-playbooks/{route.playbook_id}/artifact.json"
        )
    for route in (parent, root):
        commands.append(f"aq playbook v2-import --path reviewed-playbooks/{route.playbook_id}")
    for route in (parent, root):
        commands.append(
            f"aq playbook activate --playbook-id {route.playbook_id} "
            f"--artifact-sha256 {route.artifact.artifact_sha256} --enabled"
        )
    for route in (parent, root):
        commands.append(f"aq playbook activation-health --playbook-id {route.playbook_id}")
    scope = "once per install" if parent.scope == "system" else "once for this project"
    return Step(
        f"Import and activate the reviewed routes ({scope})",
        tuple(commands),
        "Skip when activation-health already reports these exact hashes enabled and ready.",
    )


def supported_validation(commands: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(supported, refused)``: development validation runs only server presets.

    The publisher submits each command to the job queue, which maps it onto a
    fixed preset (``src.jobs.adapters.finite_command``: ``pytest``/``aq test``
    and ``ruff check`` in the daemon's interpreter, ``npm run build`` with no
    arguments) and runs no shell.  Anything else is refused, which defers every
    batch forever rather than failing loudly, so the planner never prints one.
    """
    from src.jobs.adapters import finite_command
    from src.jobs.policy import JobError

    supported: list[str] = []
    refused: list[str] = []
    for command in commands:
        try:
            finite_command(command)
        except JobError:
            refused.append(command)
        else:
            supported.append(command)
    return supported, refused


def _development_plan(
    facts: ProjectFacts,
    classification: Classification,
    problems: list[str],
    validation: Sequence[str],
    interval_seconds: int,
) -> OnboardingPlan:
    project = facts.project_id
    supported, refused = supported_validation(validation)
    for command in refused:
        problems.append(
            f"validation command {command!r} is not a job preset (pytest, aq test, ruff check, "
            "npm run build with no arguments; no shell) and would defer every batch; "
            "it is left out"
        )
    plan = OnboardingPlan(
        project_id=project,
        shape=classification.shape,
        path="development",
        required_checks=(),
        check_version=None,
        credential_mode="n/a",
        policy=None,
        trust_manifest=None,
        validation_commands=tuple(supported),
        workflows=classification.workflows,
        problems=problems,
    )
    where = "a repository on disk" if classification.shape == "local_remote" else "another forge"
    if facts.blocked_tasks:
        plan.steps.append(
            Step(
                "Deliver BLOCKED tasks first",
                tuple(
                    command
                    for task_id in facts.blocked_tasks
                    for command in (
                        (
                            f"aq task deliver --task-id {_q(task_id)} --reason "
                            "'deliver before the development train' --dry-run"
                        ),
                        (
                            f"aq task deliver --task-id {_q(task_id)} --reason "
                            "'deliver before the development train'"
                        ),
                    )
                ),
                "task_deliver refuses a development project, so deliver these now.",
            )
        )
    if facts.current_mode == "development":
        plan.steps.append(
            Step("Already on the development train", (f"aq integration status {_q(project)}",))
        )
        return plan
    if supported:
        flags = "--validation focused " + " ".join(
            f"--command {_q(command)}" for command in supported
        )
        gate = "the listed job presets"
    else:
        flags = "--validation none"
        gate = (
            "no batch validation: no job preset can install and test this stack, so each "
            "task's own close checks remain the gate, as under per-task direct delivery"
        )
    plan.steps.append(
        Step(
            "Enter the development train",
            (
                (
                    f"aq integration develop {_q(project)} {flags} "
                    f"--interval-seconds {interval_seconds} "
                    "--reason 'onto the development train: batched, lease-guarded delivery'"
                ),
                f"aq integration status {_q(project)}",
            ),
            f"Hierarchy and train need github.com; {project} pushes to {where}, so its train "
            f"is the development publisher. Validation: {gate}.",
        )
    )
    return plan


__all__ = [
    "AuditFallback",
    "Classification",
    "JobChecks",
    "OnboardingPlan",
    "ProjectFacts",
    "Step",
    "WorkflowReport",
    "analyze_workflow",
    "audit_fallback",
    "audit_workflow_template",
    "branch_filter_matches",
    "build_policy",
    "build_trust_manifest",
    "bundle_route",
    "check_set_version",
    "ci_workflow_template",
    "classify",
    "detect_stack",
    "embedded_verifier",
    "hosted_verifier_source",
    "job_check_names",
    "job_runs_on_push",
    "plan_onboarding",
    "producer_for",
    "select_routes",
    "suggested_commands",
    "supported_validation",
    "trigger_fix",
]
