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
``docs/config/train-onboarding.md``.
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

#: The GitHub Actions App.  Existing-login credential mode matches it by slug;
#: App credential mode compares the policy producer with the trust manifest's
#: numeric ``ci_producer_app_id`` (``src/integration/preflight.py``).
GITHUB_ACTIONS_SLUG = "github-actions"
GITHUB_ACTIONS_APP_ID = 15368
ATTESTATION_NAME = "Agent Queue Integration Attestation"
TRUST_MANIFEST_PATH = ".github/agent-queue-integration.json"

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

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(name for job in self.jobs if job.status == "required" for name in job.names)

    @property
    def is_ci_candidate(self) -> bool:
        """A pull-request workflow that is not a deployment: CI that could gate the train."""
        return self.pull_request and not self.deployment


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


# Three-valued evaluation of a job ``if:`` for a push event.  Only
# ``github.event_name`` comparisons are known; every other atom is unknown.
_TOKEN = re.compile(
    r"\s*(?:(?P<op>&&|\|\||==|!=|!|\(|\)|,)|(?P<str>'(?:[^']|'')*')"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_.\-*]*)|(?P<num>-?\d+(?:\.\d+)?))"
)


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
            if left[0] == "event" and right[0] == "str":
                equal = right[1] == self.event
            elif right[0] == "event" and left[0] == "str":
                equal = left[1] == self.event
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
                while True:
                    token = self._take()
                    if token == ("op", "("):
                        depth += 1
                    elif token == ("op", ")"):
                        depth -= 1
                        if depth == 0:
                            break
                return "unknown", ""
            if value == "github.event_name":
                return "event", ""
            if value in ("true", "false"):
                return "bool", value
            return "unknown", ""
        return "unknown", value


def job_runs_on_push(condition: Any) -> bool | None:
    """``True``/``False`` when a job ``if:`` is decided for a push event, else ``None``."""
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


def analyze_workflow(path: str, text: str) -> WorkflowReport:
    """Read one workflow file: its triggers and the check runs each job produces."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return WorkflowReport(
            path=path,
            name=path,
            push_on_train_refs=False,
            pull_request=False,
            push=False,
            deployment=False,
            jobs=(),
            notes=(f"not valid YAML: {exc}",),
        )
    if not isinstance(document, Mapping):
        return WorkflowReport(
            path=path,
            name=path,
            push_on_train_refs=False,
            pull_request=False,
            push=False,
            deployment=False,
            jobs=(),
            notes=("not a workflow mapping",),
        )
    triggers = _triggers(document)
    notes: list[str] = []
    covered, problem = _push_covers_train_refs(triggers)
    if problem:
        notes.append(problem)
    raw_jobs = document.get("jobs") if isinstance(document.get("jobs"), Mapping) else {}
    deployment = any(
        isinstance(job, Mapping) and job.get("environment") is not None for job in raw_jobs.values()
    )
    if deployment and covered:
        notes.append("deployment workflow runs on the train's refs: every candidate push deploys")
    pull_request = "pull_request" in triggers or "pull_request_target" in triggers
    # A workflow gates the train when it runs on the train's refs, or could once
    # its push trigger names them (pull-request CI).  Anything else -- a
    # workflow_run follow-up, a main-only post-merge job -- never does.
    gate = covered or (pull_request and not deployment)
    jobs: list[JobChecks] = []
    for job_id, job in raw_jobs.items():
        if not isinstance(job, Mapping):
            continue
        names, unresolved = job_check_names(str(job_id), job)
        if deployment:
            jobs.append(JobChecks(str(job_id), names, "excluded", "deployment workflow"))
            continue
        if not gate:
            jobs.append(
                JobChecks(str(job_id), names, "excluded", "workflow never runs on the train's refs")
            )
            continue
        if unresolved:
            jobs.append(JobChecks(str(job_id), names, "unresolved", unresolved))
            continue
        runs = job_runs_on_push(job.get("if"))
        if runs is False:
            jobs.append(JobChecks(str(job_id), names, "excluded", "job if: skips push events"))
        elif runs is None:
            jobs.append(
                JobChecks(
                    str(job_id),
                    names,
                    "required",
                    "job if: could not be decided for push; confirm it runs on the train's refs",
                )
            )
        else:
            jobs.append(JobChecks(str(job_id), names, "required"))
        runs_on = job.get("runs-on")
        labels = runs_on if isinstance(runs_on, list) else [runs_on]
        if "self-hosted" in labels:
            notes.append(f"job {job_id} runs on a self-hosted runner, which must be online")
    return WorkflowReport(
        path=path,
        name=str(document.get("name") or path),
        push_on_train_refs=covered,
        pull_request=pull_request,
        push="push" in triggers,
        deployment=deployment,
        jobs=tuple(jobs),
        notes=tuple(notes),
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
            else ("unsupported_forge")
        )
        return Classification(shape, None, CheckSet((), ()), (), reports, tuple(problems))

    live = [report for report in reports if report.push_on_train_refs and report.required_names]
    if live:
        names = _unique(name for report in live for name in report.required_names)
        # Pull-request CI beside the live workflows cannot gate the train until
        # its push trigger names the train's refs; say so, but the shape holds.
        advisory = tuple(
            report.path
            for report in reports
            if report.is_ci_candidate and not report.push_on_train_refs and report.required_names
        )
        return Classification(
            "github_ci",
            full_name,
            CheckSet(names, tuple(r.path for r in live)),
            advisory,
            reports,
            tuple(problems),
        )
    candidates = [report for report in reports if report.is_ci_candidate and report.required_names]
    if candidates:
        names = _unique(name for report in candidates for name in report.required_names)
        return Classification(
            "github_ci_trigger_missing",
            full_name,
            CheckSet(names, tuple(r.path for r in candidates)),
            tuple(r.path for r in candidates),
            reports,
            tuple(problems),
        )
    return Classification("github_no_ci", full_name, CheckSet((), ()), (), reports, tuple(problems))


def _unique(names: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))


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
    if credential_mode not in CREDENTIAL_MODES:
        raise ValueError(f"credential mode must be one of {', '.join(CREDENTIAL_MODES)}")
    return str(GITHUB_ACTIONS_APP_ID) if credential_mode == "app" else GITHUB_ACTIONS_SLUG


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
    """``.github/agent-queue-integration.json`` for App credential mode."""
    from src.integration.ci import IntegrationTrustManifest

    manifest = {
        "schema": "aq.integration-trust.v1",
        "canonical_repository_id": canonical_repository_id,
        "repository_id": github_repository_id,
        "full_name": full_name,
        "ci_producer_app_id": GITHUB_ACTIONS_APP_ID,
        "attestation_app_id": attestation_app_id,
        "attestation_name": ATTESTATION_NAME,
        "required_checks": {"version": check_version, "names": list(checks)},
    }
    IntegrationTrustManifest.model_validate(manifest)
    return manifest


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


def validation_command(files: Mapping[str, str]) -> str | None:
    """One ``bash -c`` validation command for the development publisher.

    It runs in AQ's retained clone with the daemon's environment, so a Python
    project installs into a throwaway virtualenv outside the clone rather than
    into the daemon's own interpreter.  Node installs land in ``node_modules/``,
    which the clone's ``.gitignore`` must cover: validation that leaves the
    tree dirty refuses publication.
    """
    if detect_stack(files) == "python":
        install = _python_install(files).replace("python -m pip", '"$venv/bin/pip"', 1)
        return (
            'venv="$(mktemp -d)/venv" && python3 -m venv "$venv" && '
            f'{install} && "$venv/bin/python" -m pytest -q'
        )
    commands = suggested_commands(files)
    return " && ".join(commands) if commands else None


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
# aq/integration/** (root candidates) and aq/parent/** (parent snapshots).
# Keep `Tests` as the job name: it is the required check in the project's
# train policy. Never add deploy steps or secrets to this workflow.
on:
  pull_request:
  push:
    branches:
      - 'aq/integration/**'
      - 'aq/parent/**'
  workflow_dispatch:

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
    """The ``on.push.branches`` the workflow needs, keeping its current branches."""
    triggers = _triggers(yaml.safe_load(text) or {})
    current: list[str] = []
    push = triggers.get("push")
    if isinstance(push, Mapping):
        branches = push.get("branches")
        current = [branches] if isinstance(branches, str) else [str(b) for b in branches or ()]
    wanted = list(dict.fromkeys([*current, *TRAIN_BRANCH_PATTERNS]))
    lines = "\n".join(f"      - '{pattern}'" for pattern in wanted)
    return f"# {report.path}\non:\n  push:\n    branches:\n{lines}"


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
    validation_command: str | None
    workflows: tuple[WorkflowReport, ...]
    problems: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)

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
    validation: str | None = None,
    interval_seconds: int = 300,
    policy_path: str = "train-policy.json",
    manifest_path: str = TRUST_MANIFEST_PATH,
    workflow_path: str = ".github/workflows/ci.yml",
) -> OnboardingPlan:
    """Every step from the project's current state to a ready observe-mode train."""
    project = facts.project_id
    shape = classification.shape
    problems = list(classification.problems)
    if shape in ("local_remote", "unsupported_forge"):
        return _development_plan(facts, classification, problems, validation, interval_seconds)

    names = tuple(check_names) if check_names else classification.required.names
    version = check_version or (check_set_version(names) if names else None)
    policy = None
    manifest = None
    if names and parent_route is not None and root_route is not None:
        policy = build_policy(
            checks=names,
            check_version=version or check_set_version(names),
            producer_id=producer_for(credential_mode),
            parent_route=parent_route,
            root_route=root_route,
            intelligence_class=intelligence_class,
            profile_id=profile_id,
        )
    if credential_mode == "app" and names and classification.full_name:
        if attestation_app_id is None:
            problems.append("App credential mode needs the App id (integration.github_app.app_id)")
        elif github_repository_id is None:
            problems.append(
                "App credential mode needs the GitHub repository id: pass "
                f'--github-repository-id "$(gh api repos/{classification.full_name} --jq .id)"'
            )
        else:
            manifest = build_trust_manifest(
                canonical_repository_id=_repository_id(facts),
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
        validation_command=validation if shape == "github_no_ci" else None,
        workflows=classification.workflows,
        problems=problems,
    )
    steps = plan.steps
    if shape == "github_no_ci":
        steps.append(
            Step(
                "Land a CI workflow first",
                (),
                f"The train accepts only GitHub check runs, and {project} has no workflow. "
                f"Commit the template written to {workflow_path} through the project's current "
                "delivery path (disabled mode: its pull request, merged by a human), make its "
                "`Tests` job green on main, then run onboard-train again. It classifies as "
                "github_ci once the workflow is on the default branch.",
            )
        )
        if validation:
            steps.append(
                Step(
                    "Optional interim: the development train until CI lands",
                    (
                        (
                            f"aq integration develop {_q(project)} --validation focused --command "
                            f"{_q(validation)} --interval-seconds {interval_seconds} "
                            "--reason 'development train until the CI workflow lands'"
                        ),
                    ),
                    "Only if batched delivery is wanted before CI exists: it publishes to the "
                    "default branch after this local validation, with no pull request. The "
                    "train cutover then starts by draining it. Skip it to keep the current "
                    "pull request review until the workflow lands.",
                )
            )
        return plan
    if shape == "github_ci_trigger_missing":
        steps.append(
            Step(
                "Make CI run on the train's refs first",
                (),
                "The train reads CI only from push runs on aq/integration/** and aq/parent/**, "
                "and these workflows push only on other branches: "
                + ", ".join(classification.trigger_fixes)
                + ". Land the trigger change printed under 'Trigger fixes' through the "
                "project's current delivery path, then run onboard-train again.",
            )
        )
    if facts.legacy_pull_requests:
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
                "These completed tasks predate the train and are not enrolled in it: status "
                "ignores them and no batch will ever deliver them. Merge each approved, open "
                "PR now, while the project still uses its legacy review path (--method merge "
                "keeps the task's branch tip an ancestor of the default branch), or close it "
                "and refile the work as a new task after cutover. `aq git pr-merge` runs gh in "
                "the daemon's data directory, never in a project checkout.",
            )
        )
    if credential_mode == "app" and classification.full_name:
        steps.append(
            Step(
                "App credential mode: publish the trust manifest and Actions variables",
                (
                    (
                        f"# commit {manifest_path} (written by --write-trust-manifest) to "
                        f"{facts.default_branch} through the project's current delivery path"
                    ),
                    (
                        f"gh variable set AQ_INTEGRATION_ATTESTATION_APP_ID --repo "
                        f"{classification.full_name} --body {attestation_app_id or 'APP_ID'}"
                    ),
                    (
                        f"gh variable set AQ_INTEGRATION_REQUIRED_CHECK_VERSION --repo "
                        f"{classification.full_name} --body {version}"
                    ),
                ),
                "With integration.github_app configured the functional preflight reads "
                f"{TRUST_MANIFEST_PATH} from the default branch and both Actions variables, and "
                "requires the policy producer to be the numeric GitHub Actions App id.",
            )
        )
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
    if mode not in (None, "disabled"):
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
    repository_id = _repository_id(facts)
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
            ),
            "Adopt or bind only what the dry runs prove; both are safe to repeat.",
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


def _repository_id(facts: ProjectFacts) -> str:
    return facts.integration_repository_id or facts.project_id


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


def _development_plan(
    facts: ProjectFacts,
    classification: Classification,
    problems: list[str],
    validation: str | None,
    interval_seconds: int,
) -> OnboardingPlan:
    project = facts.project_id
    plan = OnboardingPlan(
        project_id=project,
        shape=classification.shape,
        path="development",
        required_checks=(),
        check_version=None,
        credential_mode="n/a",
        policy=None,
        trust_manifest=None,
        validation_command=validation,
        workflows=classification.workflows,
        problems=problems,
    )
    where = "a repository on disk" if classification.shape == "local_remote" else "another forge"
    if not validation:
        problems.append("no validation command: pass --validation-command")
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
    command = f"--command {_q(validation)} " if validation else "--command 'VALIDATION' "
    plan.steps.append(
        Step(
            "Enter the development train",
            (
                (
                    f"aq integration develop {_q(project)} --validation focused {command}"
                    f"--interval-seconds {interval_seconds} "
                    "--reason 'onto the development train: batched, validated delivery'"
                ),
                f"aq integration status {_q(project)}",
            ),
            f"Hierarchy and train need github.com; {project} pushes to {where}, so its train "
            "is the development publisher. The validation command runs under bash in AQ's "
            "retained clone with the daemon's environment: confirm node/python are on the "
            "daemon's PATH, or use absolute paths.",
        )
    )
    return plan


__all__ = [
    "Classification",
    "JobChecks",
    "OnboardingPlan",
    "ProjectFacts",
    "Step",
    "WorkflowReport",
    "analyze_workflow",
    "branch_filter_matches",
    "build_policy",
    "build_trust_manifest",
    "bundle_route",
    "check_set_version",
    "ci_workflow_template",
    "classify",
    "detect_stack",
    "job_check_names",
    "job_runs_on_push",
    "plan_onboarding",
    "producer_for",
    "select_routes",
    "suggested_commands",
    "trigger_fix",
    "validation_command",
]
