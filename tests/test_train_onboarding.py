"""`aq integration onboard-train`: shapes, check names, policy and plan.

The fixtures under ``tests/fixtures/train_onboarding/`` are the workflow and
stack files of the real projects the planner was written for, copied from
their default branches on 2026-09-27.  The planner must also reproduce the
reviewed agent-queue policy from this repository's own workflows.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from click.testing import CliRunner

from src.cli.exceptions import ScopeDeniedError
from src.integration import train_onboarding as onboarding
from src.integration.ci import IntegrationTrustManifest, is_numeric_producer_id
from src.integration.models import HierarchicalIntegrationPolicy, deprecated_route_fields
from src.playbooks.required import reviewed_bundle_source

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "train_onboarding"
BUNDLES = reviewed_bundle_source()

GITHUB = "https://github.com/ElectricJack/{}.git"


def _workflows(project: str) -> dict[str, str]:
    return {
        f".github/workflows/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted((FIXTURES / project).glob("*.yml"))
    }


def _files(project: str) -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((FIXTURES / project).iterdir())
        if path.suffix != ".yml"
    }


def _own_workflows() -> dict[str, str]:
    return {
        f".github/workflows/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    }


def _shared_routes():
    return onboarding.select_routes(BUNDLES, "outrider-ide", "auto")


# ---------------------------------------------------------------------------
# The real projects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("project", "repository", "shape", "names"),
    [
        (
            "outrider-ide",
            "outrider-ide",
            "github_ci",
            ("Rust 1.89 minimum version", "ubuntu-latest", "windows-latest", "macos-latest"),
        ),
        (
            "matter-engine-cpp",
            "matter-engine",
            "github_ci",
            ("Native Windows build and tests",),
        ),
        (
            "moss-and-spade-inventory-manager",
            "moss-and-spade-inventory-manager",
            "github_ci_trigger_missing",
            ("Build & test",),
        ),
        (
            "jackkern.com",
            "jackkern.com",
            "github_ci_trigger_missing",
            ("Test, build, capture, budgets, links", "Lighthouse budget"),
        ),
    ],
)
def test_github_projects_classify_with_their_real_check_names(project, repository, shape, names):
    classification = onboarding.classify(GITHUB.format(repository), _workflows(project))

    assert classification.shape == shape
    assert classification.required.names == names
    assert classification.full_name == f"ElectricJack/{repository}"
    assert classification.problems == ()


def test_github_projects_without_workflows_have_no_ci():
    for project in ("quilt-trader", "rom-downloader"):
        classification = onboarding.classify(GITHUB.format(project), {})
        assert classification.shape == "github_no_ci"
        assert classification.required.names == ()


@pytest.mark.parametrize("project", ["matter-engine-web", "quilt-trader-web", "agent-queue-site"])
def test_local_remote_projects_take_the_development_path(project):
    url = f"/home/jkern/.agent-queue/local-remotes/{project}.git"
    classification = onboarding.classify(
        url, _workflows(project) if (FIXTURES / project).is_dir() else {}
    )

    assert classification.shape == "local_remote"
    assert classification.full_name is None


def test_deploy_workflows_never_become_required_checks():
    jackkern = onboarding.classify(GITHUB.format("jackkern.com"), _workflows("jackkern.com"))
    deploy = next(r for r in jackkern.workflows if r.path.endswith("deploy.yml"))
    assert deploy.deployment
    assert {job.status for job in deploy.jobs} == {"excluded"}
    assert not deploy.push_on_train_refs

    # agent-queue-site deploys on every push: a candidate push would deploy.
    site = onboarding.classify("/srv/site.git", _workflows("agent-queue-site"))
    site_deploy = next(r for r in site.workflows if r.path.endswith("deploy.yml"))
    assert site_deploy.push_on_train_refs
    assert any("deploys on every candidate push" in note for note in site_deploy.notes)


def test_self_hosted_runners_are_named():
    matter = onboarding.classify(GITHUB.format("matter-engine"), _workflows("matter-engine-cpp"))
    assert any("self-hosted" in note for note in matter.workflows[0].notes)


@pytest.mark.parametrize("mode", onboarding.CREDENTIAL_MODES)
def test_agent_queue_workflows_reproduce_the_reviewed_policy(mode):
    classification = onboarding.classify(GITHUB.format("agent-queue"), _own_workflows())
    parent, root = onboarding.select_routes(BUNDLES, "agent-queue", "auto")
    facts = onboarding.ProjectFacts(
        "agent-queue", GITHUB.format("agent-queue"), integration_repository_id="agent-queue2"
    )

    plan = onboarding.plan_onboarding(
        facts,
        classification,
        parent_route=parent,
        root_route=root,
        credential_mode=mode,
    )

    assert classification.shape == "github_ci"
    assert parent.playbook_id == "agent-queue-parent-integration"
    assert root.playbook_id == "agent-queue-root-train"
    expected = json.loads((ROOT / "docs/config/agent-queue-train-policy.json").read_text())
    assert plan.policy == expected
    # Onboarding writes class hints only; the reviewed policy names no profile.
    policy = HierarchicalIntegrationPolicy.model_validate(plan.policy)
    assert deprecated_route_fields(policy) == []
    assert "profile_id" not in json.dumps(plan.policy)


def test_onboard_train_no_longer_takes_a_harness():
    """The class is a hint for the router; there is no rung to pick a harness for."""
    from src.cli.app import cli

    result = CliRunner().invoke(
        cli, ["integration", "onboard-train", "agent-queue", "--harness", "codex"]
    )
    assert result.exit_code == 2, result.output
    assert "No such option '--harness'" in result.output


# ---------------------------------------------------------------------------
# Triggers, conditions and names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("push", "covered"),
    [
        (None, True),
        ({"branches": ["aq/**"]}, True),
        ({"branches": ["aq/*"]}, False),
        ({"branches": ["main"]}, False),
        ({"branches": ["main", "aq/integration/**", "aq/parent/**"]}, True),
        ({"branches": ["**", "!aq/parent/**"]}, False),
        ({"branches-ignore": ["aq/integration/**"]}, False),
        ({"branches-ignore": ["gh-pages"]}, True),
        ({"tags": ["v*"]}, False),
    ],
)
def test_push_filters_must_cover_both_train_refs(push, covered):
    workflow = yaml.safe_dump({"on": {"push": push}, "jobs": {"t": {"runs-on": "x"}}})
    assert onboarding.analyze_workflow("w.yml", workflow).push_on_train_refs is covered


def test_bare_on_key_and_list_forms_are_read():
    assert onboarding.analyze_workflow("w.yml", "on: push\njobs: {}\n").push_on_train_refs
    assert onboarding.analyze_workflow("w.yml", "on: [pull_request]\njobs: {}\n").pull_request


def test_path_filtered_push_cannot_gate_the_train():
    workflow = "on:\n  push:\n    paths: ['src/**']\njobs:\n  t: {runs-on: x}\n"
    report = onboarding.analyze_workflow("w.yml", workflow)
    assert not report.push_on_train_refs
    assert any("path-filtered" in note for note in report.notes)


@pytest.mark.parametrize(
    ("condition", "runs"),
    [
        (None, True),
        ("github.event_name == 'pull_request'", False),
        ("github.event_name != 'pull_request'", True),
        ("${{ github.event_name == 'push' }}", True),
        ("github.event_name != 'pull_request' || (!github.event.pull_request.draft)", True),
        ("github.event_name == 'pull_request' && always()", False),
        ("needs.guard.outputs.configured == 'true'", None),
        ("startsWith(github.ref, 'refs/heads/aq/')", None),
        ("!(github.event_name == 'push')", False),
        ("github.ref == 'refs/heads/main'", False),
        ("github.ref_name != 'main'", True),
        ("github.ref == 'refs/heads/aq/integration/x'", None),
        ("always()", True),
        ("success() && github.event_name == 'push'", True),
        ("failure()", False),
        ("!cancelled()", True),
    ],
)
def test_job_conditions_are_decided_for_push_events(condition, runs):
    assert onboarding.job_runs_on_push(condition) is runs


def test_matrix_names_follow_github():
    job = {
        "strategy": {
            "matrix": {
                "os": ["ubuntu", "windows"],
                "py": ["3.11", "3.12"],
                "exclude": [{"os": "windows", "py": "3.11"}],
                "include": [{"os": "ubuntu", "extra": "cov"}, {"os": "macos", "py": "3.12"}],
            }
        }
    }
    names, problem = onboarding.job_check_names("test", job)
    assert problem is None
    assert names == (
        "test (ubuntu, 3.11, cov)",
        "test (ubuntu, 3.12, cov)",
        "test (windows, 3.12)",
        "test (macos, 3.12)",
    )

    named = {
        "name": "Tests (${{ matrix.suite.name }})",
        "strategy": {"matrix": {"suite": [{"name": "a"}, {"name": "b"}]}},
    }
    assert onboarding.job_check_names("test", named) == (("Tests (a)", "Tests (b)"), None)


@pytest.mark.parametrize(
    ("job", "reason"),
    [
        ({"uses": "./.github/workflows/reusable.yml"}, "reusable workflow"),
        ({"strategy": {"matrix": "${{ fromJSON(needs.plan.outputs.m) }}"}}, "expression"),
        ({"strategy": {"matrix": {"cfg": [{"a": 1}]}}}, "objects"),
        ({"name": "${{ needs.x.outputs.name }}"}, "not a matrix value"),
    ],
)
def test_underivable_names_are_reported_not_guessed(job, reason):
    names, problem = onboarding.job_check_names("job", job)
    assert names == ()
    assert reason in problem


def test_unresolved_jobs_in_a_gating_workflow_are_problems():
    workflow = "on: push\njobs:\n  a:\n    uses: ./x.yml\n  b:\n    runs-on: x\n"
    classification = onboarding.classify(GITHUB.format("p"), {"ci.yml": workflow})
    assert classification.shape == "github_ci"
    assert classification.required.names == ("b",)
    assert classification.problems == (
        ("ci.yml: a: calls a reusable workflow; its check names are 'caller / callee'"),
    )


def test_trigger_fix_makes_pull_request_ci_gate_the_train():
    path = ".github/workflows/ci.yml"
    text = _workflows("moss-and-spade-inventory-manager")[path]
    report = onboarding.analyze_workflow(path, text)
    fix = onboarding.trigger_fix(report, text)
    patched = yaml.safe_load(text)
    patched[True]["push"] = yaml.safe_load(fix.split("\n", 1)[1])[True]["push"]

    assert "'main'" in fix and "'aq/integration/**'" in fix and "'aq/parent/**'" in fix
    fixed = onboarding.classify(
        GITHUB.format("moss"), {path: yaml.safe_dump({"on": patched.pop(True), **patched})}
    )
    assert fixed.shape == "github_ci"
    assert fixed.required.names == ("Build & test",)


# ---------------------------------------------------------------------------
# Routes, policy and trust manifest
# ---------------------------------------------------------------------------


def test_shared_routes_are_the_system_scoped_reviewed_bundles():
    parent, root = _shared_routes()
    for route, playbook_id in ((parent, "parent-integration"), (root, "root-train")):
        manifest = yaml.safe_load(
            (BUNDLES / playbook_id / "manifest.md").read_text().split("---", 2)[1]
        )
        assert route.playbook_id == playbook_id
        assert route.scope == "system" and route.scope_identifier == ""
        assert route.artifact.artifact_sha256 == manifest["artifact_sha256"]
        assert route.artifact.source_digest == manifest["source_sha256"]
        assert route.artifact.contract_fingerprint == manifest["contract_fingerprint"]
        assert route.is_available_to_project("any-project")


def test_route_selection_refuses_a_missing_project_pair():
    with pytest.raises(ValueError, match="no reviewed bundle"):
        onboarding.select_routes(BUNDLES, "outrider-ide", "project")
    with pytest.raises(ValueError):
        onboarding.select_routes(BUNDLES, "outrider-ide", "bogus")


@pytest.mark.parametrize("mode", onboarding.CREDENTIAL_MODES)
def test_policy_producer_is_the_numeric_id_in_every_credential_mode(mode):
    parent, root = _shared_routes()
    policy = onboarding.build_policy(
        checks=("Tests",),
        check_version="ci-x",
        producer_id=onboarding.producer_for(mode),
        parent_route=parent,
        root_route=root,
        intelligence_class="standard-high",
    )
    validated = HierarchicalIntegrationPolicy.model_validate(policy)
    # Class hints only: the router assigns repair and verifier profiles, and a
    # bind naming the deprecated profile fields is refused.
    assert deprecated_route_fields(validated) == []
    for boundary in ("parent", "root"):
        assert "primary_profile_id" not in policy[boundary]
        assert "verifier_profile_id" not in policy[boundary]
        assert "debug_profile_id" not in policy[boundary]["repair"]
        assert policy[boundary]["primary_intelligence_class"] == "standard-high"
        assert policy[boundary]["verifier_intelligence_class"] == "standard-high"
        assert policy[boundary]["repair"]["debug_intelligence_class"] == "standard-high"
    # One producer, so a project moves between credential modes without a rebind.
    assert validated.parent.required_checks.producer_id == "15368"
    assert validated.root.required_checks.producer_id == "15368"
    assert is_numeric_producer_id(onboarding.producer_for(mode))
    assert validated.parent.route.playbook_id == "parent-integration"
    assert validated.branchless_parent == "verifier"


def test_empty_check_set_is_refused():
    parent, root = _shared_routes()
    with pytest.raises(ValueError, match="at least one required check"):
        onboarding.build_policy(
            checks=(),
            check_version="v",
            producer_id="github-actions",
            parent_route=parent,
            root_route=root,
            intelligence_class="c",
        )


def test_check_set_version_moves_with_the_names():
    assert onboarding.check_set_version(["a", "b"]) == onboarding.check_set_version(("a", "b"))
    assert onboarding.check_set_version(["a", "b"]) != onboarding.check_set_version(["b", "a"])


def test_app_mode_plan_carries_a_valid_trust_manifest_and_variables():
    classification = onboarding.classify(GITHUB.format("outrider-ide"), _workflows("outrider-ide"))
    parent, root = _shared_routes()
    facts = onboarding.ProjectFacts(
        "outrider-ide", GITHUB.format("outrider-ide"), current_mode="disabled"
    )
    plan = onboarding.plan_onboarding(
        facts,
        classification,
        parent_route=parent,
        root_route=root,
        credential_mode="app",
        attestation_app_id=5075923,
        github_repository_id=4242,
    )

    manifest = IntegrationTrustManifest.model_validate(plan.trust_manifest)
    assert manifest.ci_producer_app_id == 15368
    assert manifest.attestation_app_id == 5075923
    assert manifest.canonical_repository_id == "outrider-ide"
    assert manifest.required_checks.names == classification.required.names
    assert plan.policy["root"]["required_checks"]["producer_id"] == "15368"
    commands = "\n".join(c for step in plan.steps for c in step.commands)
    assert (
        f"AQ_INTEGRATION_REQUIRED_CHECK_VERSION --repo ElectricJack/outrider-ide --body {plan.check_version}"
        in commands
    )

    unresolved = onboarding.plan_onboarding(
        facts,
        classification,
        parent_route=parent,
        root_route=root,
        credential_mode="app",
        attestation_app_id=5075923,
    )
    assert unresolved.trust_manifest is None
    assert any("GitHub repository id" in problem for problem in unresolved.problems)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _titles(plan) -> list[str]:
    return [step.title for step in plan.steps]


def test_github_ci_plan_from_development_drains_then_binds_in_order():
    classification = onboarding.classify(
        GITHUB.format("matter-engine"), _workflows("matter-engine-cpp")
    )
    parent, root = _shared_routes()
    facts = onboarding.ProjectFacts(
        "matter-engine-cpp",
        GITHUB.format("matter-engine"),
        integration_repository_id="matter-engine-cpp",
        current_mode="development",
    )
    plan = onboarding.plan_onboarding(facts, classification, parent_route=parent, root_route=root)

    titles = _titles(plan)
    assert (
        titles.index("Drain the development publisher")
        < titles.index("Bind repository, review mode and policy (disabled and drained)")
        < titles.index("Observe: preflight without scheduling")
        < titles.index("Ready check")
    )
    commands = [c for step in plan.steps for c in step.commands]
    assert any("integration-repository-id matter-engine-cpp" in c for c in commands)
    assert all('"$(generation)"' in c for c in commands if "--expected" in c)
    assert not any("--mode train" in c or "--mode hierarchy" in c for c in commands)


def test_unbound_project_binds_its_repository_record_and_resolves_legacy_prs_first():
    classification = onboarding.classify(GITHUB.format("outrider-ide"), _workflows("outrider-ide"))
    parent, root = _shared_routes()
    facts = onboarding.ProjectFacts(
        "outrider-ide",
        GITHUB.format("outrider-ide"),
        current_mode="disabled",
        legacy_pull_requests=(
            ("prime-torrent", "https://github.com/ElectricJack/outrider-ide/pull/1"),
        ),
    )
    plan = onboarding.plan_onboarding(facts, classification, parent_route=parent, root_route=root)

    titles = _titles(plan)
    assert "Drain the disabled publisher" not in " ".join(titles)
    assert titles[0] == "Resolve legacy pull requests before the drain"
    assert (
        "aq git pr-merge --project-id outrider-ide --pr-url https://github.com/ElectricJack/outrider-ide/pull/1 --method merge"
        in plan.steps[0].commands
    )
    bind = next(s for s in plan.steps if s.title.startswith("Bind"))
    record = json.dumps(
        {"id": "outrider-ide", "url": GITHUB.format("outrider-ide"), "default_branch": "main"},
        separators=(",", ":"),
    )
    assert f"integration-repository '{record}'" in bind.commands[0]


def test_trigger_missing_plan_stops_after_the_repository_changes():
    classification = onboarding.classify(GITHUB.format("jackkern.com"), _workflows("jackkern.com"))
    parent, root = _shared_routes()
    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts("jackkern.com", GITHUB.format("jackkern.com")),
        classification,
        parent_route=parent,
        root_route=root,
        credential_mode="app",
        attestation_app_id=5075923,
        github_repository_id=7,
    )
    assert _titles(plan) == [
        "Make CI run on the train's refs first",
        "App mode: land the trust manifest and the audit workflow",
    ]
    assert plan.policy is not None
    # No repository is designated yet, so the files come from this run's writers.
    assert plan.steps[1].commands == (
        (
            "# commit .github/agent-queue-integration.json (--write-trust-manifest) and "
            ".github/workflows/main-attestation.yml (--write-audit-workflow) to main, through "
            "the project's current delivery path"
        ),
    )


def test_no_ci_plan_stops_at_the_workflow():
    classification = onboarding.classify(GITHUB.format("quilt-trader"), {})
    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts("quilt-trader", GITHUB.format("quilt-trader")),
        classification,
        parent_route=None,
        root_route=None,
    )
    assert _titles(plan) == ["Land a CI workflow first"]
    assert plan.policy is None
    assert "A local command cannot" in plan.steps[0].note


def test_ci_template_gates_the_train_with_a_tests_job():
    for project in ("quilt-trader", "rom-downloader", "matter-engine-web"):
        template = onboarding.ci_workflow_template(_files(project))
        classification = onboarding.classify(GITHUB.format(project), {"ci.yml": template})
        assert classification.shape == "github_ci"
        assert classification.required.names == ("Tests",)
        assert "${{ secrets." not in template
        assert "      - main\n" in template

    assert "exit 1" in onboarding.ci_workflow_template({})
    assert "- run: make check" in onboarding.ci_workflow_template({}, test_command="make check")
    pnpm = onboarding.suggested_commands(_files("agent-queue-site") | {"pnpm-lock.yaml": ""})
    assert pnpm == ("pnpm install --frozen-lockfile", "pnpm check")


def test_local_remote_plan_without_a_preset_publishes_unvalidated_batches():
    classification = onboarding.classify("/srv/local-remotes/matter-engine-web.git", {})
    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts(
            "matter-engine-web",
            "/srv/local-remotes/matter-engine-web.git",
            current_mode="disabled",
            blocked_tasks=("agile-flare",),
        ),
        classification,
        parent_route=None,
        root_route=None,
    )
    assert plan.path == "development"
    assert _titles(plan) == ["Deliver BLOCKED tasks first", "Enter the development train"]
    assert (
        plan.steps[1]
        .commands[0]
        .startswith(
            "aq integration develop matter-engine-web --validation none --interval-seconds 300"
        )
    )
    assert "close checks remain the gate" in plan.steps[1].note

    already = onboarding.plan_onboarding(
        onboarding.ProjectFacts("x", "/srv/x.git", current_mode="development"),
        classification,
        parent_route=None,
        root_route=None,
    )
    assert _titles(already) == ["Already on the development train"]


def test_development_validation_keeps_only_job_presets():
    from src.jobs.adapters import finite_command

    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts("x", "/srv/x.git"),
        onboarding.classify("/srv/x.git", {}),
        parent_route=None,
        root_route=None,
        validation=("npm run build", "npm ci && npm test", "python3 -m pytest -q tests"),
    )
    command = plan.steps[-1].commands[0]
    assert (
        "--validation focused --command 'npm run build' --command 'python3 -m pytest -q tests'"
        in command
    )
    assert "npm ci" not in command
    assert plan.validation_commands == ("npm run build", "python3 -m pytest -q tests")
    assert any("'npm ci && npm test' is not a job preset" in p for p in plan.problems)
    for printed in plan.validation_commands:
        finite_command(printed)


def test_main_only_deploy_job_leaves_the_rest_of_the_workflow_ci():
    workflow = """
on: push
jobs:
  test: {runs-on: x}
  deploy:
    needs: test
    if: github.ref == 'refs/heads/main'
    environment: production
    runs-on: x
"""
    report = onboarding.analyze_workflow("ci.yml", workflow)
    assert not report.deployment
    assert report.required_names == ("test",)
    assert not report.notes
    status = {job.job_id: job.status for job in report.jobs}
    assert status == {"test": "required", "deploy": "excluded"}


def test_push_only_ci_needs_a_trigger_fix_not_a_new_workflow():
    workflow = "on:\n  push:\n    branches: [main]\njobs:\n  test: {runs-on: x}\n"
    classification = onboarding.classify(GITHUB.format("p"), {"ci.yml": workflow})
    assert classification.shape == "github_ci_trigger_missing"
    assert classification.required.names == ("test",)
    assert classification.trigger_fixes == ("ci.yml",)


def test_a_job_needing_a_skipped_job_is_not_required():
    workflow = """
on: push
jobs:
  guard:
    if: github.event_name == 'pull_request'
    runs-on: x
  tests:
    needs: guard
    runs-on: x
  report:
    needs: [tests]
    runs-on: x
  summary:
    needs: guard
    if: always()
    runs-on: x
"""
    report = onboarding.analyze_workflow("ci.yml", workflow)
    status = {job.job_id: (job.status, job.reason) for job in report.jobs}
    assert status["tests"][0] == "excluded" and "needs guard" in status["tests"][1]
    assert status["report"][0] == "excluded" and "needs tests" in status["report"][1]
    assert status["summary"] == ("required", None)


def test_a_shared_check_name_is_a_problem_and_blocks_the_policy():
    one = "on: push\njobs:\n  a: {name: Tests, runs-on: x}\n"
    two = "on: push\njobs:\n  b: {name: Tests, runs-on: x}\n"
    classification = onboarding.classify(GITHUB.format("p"), {"one.yml": one, "two.yml": two})
    assert classification.required.names == ("Tests",)
    assert any("'Tests' is produced by one.yml:a, two.yml:b" in p for p in classification.problems)
    parent, root = _shared_routes()
    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts("p", GITHUB.format("p")),
        classification,
        parent_route=parent,
        root_route=root,
    )
    assert plan.policy is None
    assert _titles(plan) == ["Resolve the problems above, then run onboard-train again"]


def test_a_repository_url_binding_would_refuse_is_a_problem():
    classification = onboarding.classify(
        "git@github.com:ElectricJack/p.git", _workflows("outrider-ide")
    )
    assert classification.shape == "github_ci"
    assert any("https://github.com/ElectricJack/p.git" in p for p in classification.problems)


def test_development_project_with_an_unreported_repository_is_not_guessed():
    classification = onboarding.classify(
        GITHUB.format("matter-engine"), _workflows("matter-engine-cpp")
    )
    parent, root = _shared_routes()
    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts(
            "matter-engine-cpp", GITHUB.format("matter-engine"), current_mode="development"
        ),
        classification,
        parent_route=parent,
        root_route=root,
    )
    assert any("--repository-id" in p for p in plan.problems)
    assert _titles(plan)[-1] == "Resolve the problems above, then run onboard-train again"


def test_trigger_fix_for_branches_ignore_names_the_patterns_to_drop():
    text = "on:\n  push:\n    branches-ignore: ['aq/**', 'gh-pages']\n  pull_request:\njobs:\n  t: {runs-on: x}\n"
    report = onboarding.analyze_workflow("ci.yml", text)
    fix = onboarding.trigger_fix(report, text)
    assert "branches-ignore" in fix and "aq/**" in fix and "gh-pages" not in fix.split(":")[-1]
    assert "branches:" not in fix


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _repo(tmp_path: Path, workflows: dict[str, str], files: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for path, text in {**workflows, **(files or {})}.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "init",
    )
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _client(responses: dict[str, object]):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def execute(command, args):
        value = responses.get(command)
        if isinstance(value, Exception):
            raise value
        return value

    client.execute = AsyncMock(side_effect=execute)
    return client


def test_cli_plans_from_the_daemon_and_git_and_writes_the_policy(tmp_path):
    from src.cli.app import cli

    repo = _repo(
        tmp_path,
        {".github/workflows/ci.yml": _workflows("outrider-ide")[".github/workflows/ci.yml"]},
    )
    client = _client(
        {
            "get_project": {
                "repo_url": GITHUB.format("outrider-ide"),
                "repo_default_branch": "main",
                "workspace": str(repo),
            },
            "integration_status": {"repository_id": None, "effective_mode": "disabled"},
            "list_tasks": {
                "tasks": [
                    {
                        "id": "prime-torrent",
                        "pr_url": "https://github.com/ElectricJack/outrider-ide/pull/1",
                    },
                    {"id": "child", "parent_task_id": "p", "pr_url": "https://x/pull/9"},
                ]
            },
        }
    )
    policy_path = tmp_path / "policy.json"
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "--json",
                "integration",
                "onboard-train",
                "outrider-ide",
                "--credential-mode",
                "existing-login",
                "--no-check-prs",
                "--write-policy",
                str(policy_path),
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["shape"] == "github_ci"
    assert data["required_checks"] == [
        "Rust 1.89 minimum version",
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    ]
    assert data["source"]["ref"] == "origin/main"
    assert data["steps"][0]["title"] == "Resolve legacy pull requests before the drain"
    assert "pull/9" not in json.dumps(data["steps"][0])
    policy = HierarchicalIntegrationPolicy.model_validate(json.loads(policy_path.read_text()))
    assert policy.root.required_checks.producer_id == "15368"
    assert policy.root.route.playbook_id == "root-train"
    commands = [command for command, _args in (call.args for call in client.execute.call_args_list)]
    assert set(commands) == {"get_project", "integration_status", "list_tasks"}


@pytest.mark.parametrize("mode", onboarding.CREDENTIAL_MODES)
def test_cli_writes_the_reviewed_agent_queue_policy_byte_for_byte(tmp_path, mode):
    from src.cli.app import cli

    repo = _repo(tmp_path, _own_workflows())
    client = _client(
        {
            "get_project": {
                "repo_url": GITHUB.format("agent-queue"),
                "repo_default_branch": "main",
                "workspace": str(repo),
            },
            "integration_status": {"repository_id": "agent-queue2", "effective_mode": "disabled"},
        }
    )
    app_config = {"integration": {"github_app": {"app_id": 5075923}}}
    policy_path = tmp_path / "policy.json"
    with (
        patch("src.cli.integration._get_client", return_value=client),
        patch("src.cli.integration._daemon_config", return_value=app_config),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "--json",
                "integration",
                "onboard-train",
                "agent-queue",
                "--credential-mode",
                mode,
                "--github-repository-id",
                "1160639300",
                "--no-check-prs",
                "--write-policy",
                str(policy_path),
            ],
        )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["credential_mode"] == mode
    assert policy_path.read_bytes() == (
        ROOT / "docs/config/agent-queue-train-policy.json"
    ).read_bytes()


def test_cli_writes_the_trust_manifest_the_daemon_command_renders(tmp_path):
    """``--write-trust-manifest`` and ``trust-manifest --write`` share one builder."""
    from src.cli.app import cli
    from src.integration import trust_manifest

    repo = _repo(tmp_path, _own_workflows())
    client = _client(
        {
            "get_project": {
                "repo_url": GITHUB.format("agent-queue"),
                "repo_default_branch": "main",
                "workspace": str(repo),
            },
            "integration_status": {"repository_id": "agent-queue2", "effective_mode": "disabled"},
        }
    )
    app_config = {"integration": {"github_app": {"app_id": 5075923}}}
    manifest_path = tmp_path / "agent-queue-integration.json"
    with (
        patch("src.cli.integration._get_client", return_value=client),
        patch("src.cli.integration._daemon_config", return_value=app_config),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "--json", "integration", "onboard-train", "agent-queue",
                "--credential-mode", "app",
                "--github-repository-id", "1160639300", "--no-check-prs",
                "--write-trust-manifest", str(manifest_path),
            ],
        )

    assert result.exit_code == 0, result.output
    reviewed = json.loads((ROOT / "docs/config/agent-queue-train-policy.json").read_text())
    expected = trust_manifest.manifest_for_policy(
        reviewed,
        canonical_repository_id="agent-queue2",
        repository_id=1160639300,
        full_name="ElectricJack/agent-queue",
        attestation_app_id=5075923,
    )
    assert manifest_path.read_text() == trust_manifest.canonical_text(expected)


def test_cli_plans_from_git_alone_when_daemon_records_are_out_of_scope(tmp_path):
    from src.cli.app import cli

    repo = _repo(
        tmp_path,
        {},
        {"package.json": _files("matter-engine-web")["package.json"], "package-lock.json": "{}"},
    )
    denied = ScopeDeniedError("get_project", "out of scope: get_project")
    client = _client({"get_project": denied, "integration_status": denied, "list_tasks": denied})
    workflow_path = tmp_path / "ci.yml"
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "--json",
                "integration",
                "onboard-train",
                "matter-engine-web",
                "--repo",
                str(repo),
                "--repo-url",
                "/srv/local-remotes/matter-engine-web.git",
                "--write-workflow",
                str(workflow_path),
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["shape"] == "local_remote"
    assert data["validation_commands"] == []
    assert "--validation none" in data["steps"][-1]["commands"][0]
    assert data["written"] == [str(workflow_path)]
    assert "name: Tests" in workflow_path.read_text()


def test_cli_needs_a_repository_url_when_the_project_is_unreadable(tmp_path):
    from src.cli.app import cli

    repo = _repo(tmp_path, {})
    denied = ScopeDeniedError("get_project", "out of scope")
    client = _client({"get_project": denied, "integration_status": denied, "list_tasks": denied})
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(cli, ["integration", "onboard-train", "p", "--repo", str(repo)])
    assert result.exit_code != 0
    assert "--repo-url" in result.output

    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", GITHUB.format("p")], check=True
    )
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "--json",
                "integration",
                "onboard-train",
                "p",
                "--repo",
                str(repo),
                "--credential-mode",
                "existing-login",
            ],
        )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["shape"] == "github_no_ci"
    assert "read from the checkout's origin" in data["problems"][0]


def test_cli_human_output_keeps_commands_on_one_line(tmp_path):
    from src.cli.app import cli

    repo = _repo(
        tmp_path,
        {
            ".github/workflows/ci.yml": _workflows("matter-engine-cpp")[
                ".github/workflows/native-windows.yml"
            ]
        },
    )
    client = _client(
        {
            "get_project": None,
            "integration_status": {"repository_id": None, "effective_mode": "disabled"},
            "list_tasks": None,
        }
    )
    with patch("src.cli.integration._get_client", return_value=client):
        result = CliRunner().invoke(
            cli,
            [
                "integration",
                "onboard-train",
                "matter-engine-cpp",
                "--repo",
                str(repo),
                "--repo-url",
                GITHUB.format("matter-engine"),
                "--credential-mode",
                "existing-login",
            ],
            terminal_width=60,
        )
    assert result.exit_code == 0, result.output
    activate = [line for line in result.output.splitlines() if "aq playbook activate" in line]
    assert activate and all("--enabled" in line for line in activate)


def test_runbook_quotes_the_shipped_shared_route_hashes():
    runbook = (ROOT / "docs/config/train-onboarding.md").read_text(encoding="utf-8")
    for route in _shared_routes():
        assert (
            f"--playbook-id {route.playbook_id} --artifact-sha256 "
            f"{route.artifact.artifact_sha256} --enabled"
        ) in runbook


# ---------------------------------------------------------------------------
# App credential mode (App-mode integration train spec §11)
# ---------------------------------------------------------------------------

APP_TITLES = (
    "App mode: land the trust manifest and the audit workflow",
    "Drain the development publisher",
    "App mode: set the Actions variables (repository admin)",
    "App mode: require the attestation on main (repository admin)",
    "Bind repository, review mode and policy (disabled and drained)",
    "Observe: preflight without scheduling",
)


def _app_plan(project: str, repository: str, **facts):
    classification = onboarding.classify(GITHUB.format(repository), _workflows(project))
    parent, root = _shared_routes()
    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts(project, GITHUB.format(repository), **facts),
        classification,
        parent_route=parent,
        root_route=root,
        credential_mode="app",
        attestation_app_id=5075923,
        github_repository_id=4242,
        policy_path=f"train-policy.{project}.json",
    )
    return plan, {step.title: step for step in plan.steps}


def test_app_mode_plan_runs_manifest_drain_variables_ruleset_bind_observe_in_order():
    plan, steps = _app_plan(
        "matter-engine-cpp",
        "matter-engine",
        integration_repository_id="matter-engine-cpp",
        current_mode="development",
    )

    titles = _titles(plan)
    assert [titles.index(title) for title in APP_TITLES] == sorted(
        titles.index(title) for title in APP_TITLES
    )
    inputs = (
        "matter-engine-cpp --policy train-policy.matter-engine-cpp.json "
        "--repository-id matter-engine-cpp"
    )
    land = steps[APP_TITLES[0]]
    assert land.commands[0] == (
        f"aq integration trust-manifest {inputs} --write .github/agent-queue-integration.json"
    )
    assert "main-attestation.yml (--write-audit-workflow) to main" in land.commands[1]
    variables = steps[APP_TITLES[2]]
    assert variables.commands == (f"aq integration app-setup {inputs} --apply",)
    assert "AQ_INTEGRATION_ATTESTATION_APP_ID=5075923" in variables.note
    assert f"AQ_INTEGRATION_REQUIRED_CHECK_VERSION={plan.check_version}" in variables.note
    ruleset = steps[APP_TITLES[3]]
    assert ruleset.commands[0] == f"aq integration app-setup {inputs}"
    assert ruleset.commands[1] == (
        "gh api --method POST repos/ElectricJack/matter-engine/rulesets "
        "--input ruleset.matter-engine-cpp.json"
    )
    assert ruleset.commands[-1] == f"aq integration app-verify {inputs}"
    assert "aq integration app-verify matter-engine-cpp" in steps[APP_TITLES[5]].commands
    assert not any("gh variable set" in c for step in plan.steps for c in step.commands)

    rule = plan.ruleset["rules"][0]["parameters"]["required_status_checks"]
    assert rule == [{"context": onboarding.ATTESTATION_NAME, "integration_id": 5075923}]
    assert plan.ruleset["bypass_actors"] == []
    assert plan.ruleset["name"] == "Train-only main"


def test_app_mode_plan_before_a_repository_record_uses_this_runs_files_and_gh():
    plan, steps = _app_plan("outrider-ide", "outrider-ide", current_mode="disabled")

    titles = _titles(plan)
    expected = [title for title in APP_TITLES if not title.startswith("Drain")]
    assert [title for title in titles if title in APP_TITLES] == expected
    assert not any("aq integration trust-manifest" in c for c in steps[expected[0]].commands)
    assert steps[expected[1]].commands == (
        (
            "gh variable set AQ_INTEGRATION_ATTESTATION_APP_ID --repo ElectricJack/outrider-ide "
            "--body 5075923"
        ),
        (
            "gh variable set AQ_INTEGRATION_REQUIRED_CHECK_VERSION --repo "
            f"ElectricJack/outrider-ide --body {plan.check_version}"
        ),
    )
    assert (
        "gh api --method POST repos/ElectricJack/outrider-ide/rulesets "
        "--input ruleset.outrider-ide.json"
    ) in steps[expected[2]].commands
    assert "--write-ruleset" in steps[expected[2]].note
    assert "aq integration app-verify outrider-ide" in steps[expected[4]].commands


def test_existing_login_plan_has_no_app_steps():
    classification = onboarding.classify(GITHUB.format("outrider-ide"), _workflows("outrider-ide"))
    parent, root = _shared_routes()
    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts("outrider-ide", GITHUB.format("outrider-ide"), current_mode="disabled"),
        classification,
        parent_route=parent,
        root_route=root,
    )
    assert not any(title.startswith("App mode") for title in _titles(plan))
    assert plan.ruleset is None
    assert not any("app-verify" in c for step in plan.steps for c in step.commands)


# ---------------------------------------------------------------------------
# The main push audit workflow
# ---------------------------------------------------------------------------

VERIFIER = ROOT / "src/integration/hosted_attestation.py"


def _audit(project: str | None, workflows: dict[str, str] | None = None):
    workflows = _own_workflows() if project is None else (workflows or _workflows(project))
    url = GITHUB.format(project or "agent-queue")
    classification = onboarding.classify(url, workflows)
    fallback = onboarding.audit_fallback(classification)
    return fallback, onboarding.audit_workflow_template(fallback)


def test_audit_workflow_embeds_the_hosted_verifier_verbatim():
    _fallback, text = _audit("outrider-ide")
    assert onboarding.embedded_verifier(text) == VERIFIER.read_text(encoding="utf-8")


def test_agent_queue_audit_calls_its_ci_like_the_committed_workflow():
    fallback, text = _audit(None)
    assert fallback.calls == (".github/workflows/tests.yml",)
    assert not fallback.fails
    rendered = yaml.safe_load(text)
    committed = yaml.safe_load(
        (ROOT / ".github/workflows/main-attestation.yml").read_text(encoding="utf-8")
    )
    for key in ("name", True, "permissions"):
        assert rendered[key] == committed[key]
    assert rendered["jobs"]["unattested-ci"] == committed["jobs"]["unattested-ci"]
    attestation = rendered["jobs"]["attestation"]
    assert attestation["runs-on"] == "ubuntu-latest"
    assert {
        key: value for key, value in attestation["outputs"].items() if key != "reason"
    } == committed["jobs"]["attestation"]["outputs"]
    verify = next(step for step in attestation["steps"] if step.get("id") == "verify")
    committed_verify = committed["jobs"]["attestation"]["steps"][-1]
    assert verify["env"] == committed_verify["env"]


def test_the_audit_is_never_a_gating_workflow():
    _fallback, text = _audit(None)
    report = onboarding.analyze_workflow(onboarding.AUDIT_WORKFLOW_PATH, text)
    assert report.audit and not report.is_ci_candidate
    assert not report.required_names
    workflows = {**_workflows("jackkern.com"), onboarding.AUDIT_WORKFLOW_PATH: text}
    classification = onboarding.classify(GITHUB.format("jackkern.com"), workflows)
    assert classification.required.sources == (".github/workflows/ci.yml",)
    assert onboarding.AUDIT_WORKFLOW_PATH not in classification.trigger_fixes


def test_audit_without_a_callable_ci_fails_an_unattested_push_job():
    fallback, text = _audit("matter-engine-cpp")
    assert fallback.calls == ()
    assert fallback.uncalled == (
        (".github/workflows/native-windows.yml", "does not declare workflow_call"),
    )
    jobs = yaml.safe_load(text)["jobs"]
    assert set(jobs) == {"attestation", "unattested-push"}
    # The project's CI is self-hosted; the audit is not.
    assert jobs["attestation"]["runs-on"] == "ubuntu-latest"
    job = jobs["unattested-push"]
    assert job["needs"] == "attestation"
    assert "configured == 'true'" in job["if"] and "attested != 'true'" in job["if"]
    assert "native-windows.yml does not declare workflow_call" in job["steps"][0]["env"]["UNCALLED"]
    assert "secrets." not in text and "environment" not in text


def _gating(extra: str = "", jobs: str = "") -> str:
    return (
        "name: CI\n"
        "on:\n"
        "  pull_request:\n"
        "  push:\n"
        "    branches: [main, 'aq/integration/**', 'aq/parent/**']\n"
        f"  workflow_call:{extra}\n"
        "jobs:\n"
        "  tests:\n"
        "    name: Tests\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: pytest\n"
        f"{jobs}"
    )


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            _gating(
                jobs=(
                    "  deploy:\n"
                    "    if: github.ref == 'refs/heads/main'\n"
                    "    environment: production\n"
                    "    runs-on: ubuntu-latest\n"
                    "    steps:\n"
                    "      - run: ./deploy\n"
                )
            ),
            "declares an environment (deploy), so it may deploy",
        ),
        (
            _gating("\n    secrets:\n      TOKEN:\n        required: true"),
            "workflow_call requires secrets the audit does not pass: TOKEN",
        ),
        (
            _gating("\n    inputs:\n      target:\n        required: true\n        type: string"),
            "workflow_call requires inputs the audit does not pass: target",
        ),
        (
            _gating() + "permissions:\n  contents: write\n",
            (
                "asks for contents: write; the audit's token holds only contents: read and "
                "checks: read"
            ),
        ),
    ],
)
def test_audit_refuses_to_call_a_workflow_that_could_deploy_or_widen_its_token(text, reason):
    fallback, rendered = _audit("outrider-ide", {".github/workflows/ci.yml": text})
    assert fallback.calls == ()
    assert fallback.uncalled == ((".github/workflows/ci.yml", reason),)
    assert "uses:" not in rendered.split("  unattested-push:")[1]


def test_audit_calls_a_workflow_that_declares_workflow_call_safely():
    text = _gating("\n    inputs:\n      target:\n        type: string\n        default: all")
    fallback, rendered = _audit("outrider-ide", {".github/workflows/ci.yml": text})
    assert fallback.calls == (".github/workflows/ci.yml",)
    jobs = yaml.safe_load(rendered)["jobs"]
    assert jobs["unattested-ci"]["uses"] == "./.github/workflows/ci.yml"
    assert "unattested-push" not in jobs


def test_the_ci_template_is_callable_by_the_audit():
    template = onboarding.ci_workflow_template(_files("quilt-trader"))
    classification = onboarding.classify(
        GITHUB.format("quilt-trader"), {".github/workflows/ci.yml": template}
    )
    assert onboarding.audit_fallback(classification).calls == (".github/workflows/ci.yml",)


def test_audit_template_refuses_text_it_would_change():
    fallback = onboarding.AuditFallback((), ())
    for verifier in ("x = '${{ github.token }}'\n", "a\nAQ_HOSTED_ATTESTATION_PY\n", "a  \n"):
        with pytest.raises(ValueError):
            onboarding.audit_workflow_template(fallback, verifier=verifier)
    with pytest.raises(ValueError):
        onboarding.audit_workflow_template(fallback, default_branch="main$(id)")


def _shell(script: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-e", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_rendered_steps_run_as_written(tmp_path):
    """The verify step writes and runs the embedded verifier; the fallback fails loudly."""
    import sys

    _fallback, text = _audit("outrider-ide")
    jobs = yaml.safe_load(text)["jobs"]
    python = Path(sys.executable).parent
    output, summary = tmp_path / "output", tmp_path / "summary"
    verify = next(step for step in jobs["attestation"]["steps"] if step.get("id") == "verify")
    result = _shell(
        verify["run"],
        {
            "PATH": f"{python}:/usr/bin:/bin",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "APP_ID": "",
            "CHECK_VERSION": "",
        },
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "aq_hosted_attestation.py").read_text() == VERIFIER.read_text()
    assert "configured=false\n" in output.read_text()

    step = jobs["unattested-push"]["steps"][0]
    result = _shell(
        step["run"],
        {
            "GITHUB_STEP_SUMMARY": str(summary),
            "REASON": "no check run",
            "UNCALLED": step["env"]["UNCALLED"],
        },
        tmp_path,
    )
    assert result.returncode == 1
    assert "::error title=Unattested push to main::no check run" in result.stdout
    assert "### Unattested push to main" in summary.read_text()


def test_cli_writes_the_audit_workflow_and_ruleset_in_app_mode(tmp_path):
    from src.cli.app import cli

    repo = _repo(tmp_path, _own_workflows())
    client = _client(
        {
            "get_project": {
                "repo_url": GITHUB.format("agent-queue"),
                "repo_default_branch": "main",
                "workspace": str(repo),
            },
            "integration_status": {"repository_id": "agent-queue2", "effective_mode": "development"},
        }
    )
    app_config = {"integration": {"github_app": {"app_id": 5075923}}}
    audit, ruleset = tmp_path / "main-attestation.yml", tmp_path / "ruleset.json"
    with (
        patch("src.cli.integration._get_client", return_value=client),
        patch("src.cli.integration._daemon_config", return_value=app_config),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "--json", "integration", "onboard-train", "agent-queue",
                "--check-version", "tests-yml-v3", "--github-repository-id", "1160639300",
                "--repository-id", "agent-queue2", "--no-check-prs",
                "--write-policy", str(tmp_path / "policy.json"),
                "--write-audit-workflow", str(audit), "--write-ruleset", str(ruleset),
            ],
        )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["credential_mode"] == "app"
    assert data["audit_workflow"]["calls"] == [".github/workflows/tests.yml"]
    assert onboarding.embedded_verifier(audit.read_text()) == VERIFIER.read_text()
    assert json.loads(ruleset.read_text()) == data["ruleset"]
    titles = [step["title"] for step in data["steps"]]
    assert [title for title in titles if title in APP_TITLES] == list(APP_TITLES)


# ---------------------------------------------------------------------------
# The App-mode runbook (docs/config/app-mode-train.md)
# ---------------------------------------------------------------------------

APP_RUNBOOK = ROOT / "docs/config/app-mode-train.md"
_GLOBAL_OPTIONS = {"--json", "--brief"}
_SHELL_ENDS = {"|", ">", ";", "&&", "#", "||"}


def _runbook_commands(text: str) -> list[str]:
    """Every ``aq ...`` line of a fenced block and every inline ``aq ...`` span."""
    import re

    commands: list[str] = []
    prose: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            candidate = line.strip().removeprefix("$ ")
            if candidate.startswith("aq "):
                commands.append(candidate)
        else:
            prose.append(line)
    for span in re.findall(r"`([^`]+)`", "\n".join(prose)):
        span = " ".join(span.split())
        if span.startswith("aq "):
            commands.append(span)
    return commands


def _resolve_command(command: str, inventory: dict[str, set[str]]) -> tuple[str | None, list]:
    """``(inventory path, options used)`` for one documented ``aq`` command line."""
    import shlex

    tokens = [token.strip("[]()") for token in shlex.split(command, comments=True)]
    words: list[str] = []
    rest = iter(tokens[1:])
    for token in rest:
        if token in _GLOBAL_OPTIONS:
            continue
        if token == "--api-url":
            next(rest, None)
            continue
        words.append(token)
    path, options = None, []
    for size in range(len(words), 0, -1):
        candidate = "aq " + " ".join(words[:size])
        if candidate in inventory:
            path, remainder = candidate, words[size:]
            break
    if path is None:
        return None, []
    for index, token in enumerate(remainder):
        following = remainder[index + 1] if index + 1 < len(remainder) else ""
        if token in _SHELL_ENDS and not following.startswith("--"):
            break
        if token.startswith("--"):
            options.append(token.split("=", 1)[0])
    return path, options


def _command_problem(command: str, inventory: dict[str, set[str]]) -> str | None:
    path, options = _resolve_command(command, inventory)
    if path is None:
        return f"no CLI command: {command}"
    unknown = [
        option
        for option in options
        if option not in inventory[path]
        and option.replace("--no-", "--", 1) not in inventory[path]
    ]
    return f"{path} has no option {', '.join(unknown)}: {command}" if unknown else None


def test_every_command_in_the_app_mode_runbook_exists_in_the_cli_inventory():
    recorded = json.loads(
        (ROOT / "docs/reference/cli-command-inventory.json").read_text(encoding="utf-8")
    )
    inventory = {
        record["path"]: {
            *record["parameters"]["required"],
            *record["parameters"]["optional"],
        }
        for record in recorded["commands"]
    }
    commands = _runbook_commands(APP_RUNBOOK.read_text(encoding="utf-8"))

    assert len(commands) >= 20
    assert {
        "aq integration trust-manifest",
        "aq integration app-verify",
        "aq integration app-setup",
        "aq integration onboard-train",
        "aq doctor",
    } <= {_resolve_command(command, inventory)[0] for command in commands}
    problems = [problem for c in commands if (problem := _command_problem(c, inventory))]
    assert problems == []


def test_command_check_rejects_unknown_commands_and_options():
    inventory = {"aq integration app-verify": {"--policy", "--json"}}
    assert _command_problem("aq integration app-verify P --policy F", inventory) is None
    assert _command_problem("aq --json integration app-verify P | python3 -c x", inventory) is None
    assert "no CLI command" in _command_problem("aq integration app-check P", inventory)
    assert "no option --apply" in _command_problem(
        "aq integration app-verify P --apply", inventory
    )


def test_app_mode_docs_point_to_the_runbook():
    for doc in (
        "docs/config/train-onboarding.md",
        "docs/config/agent-queue-train-policy.md",
        "docs/guides/hierarchical-integration-trains.md",
    ):
        assert "app-mode-train.md" in (ROOT / doc).read_text(encoding="utf-8"), doc
