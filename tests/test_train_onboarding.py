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
from src.integration.ci import IntegrationTrustManifest
from src.integration.models import HierarchicalIntegrationPolicy
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
    assert any("every candidate push deploys" in note for note in site_deploy.notes)


def test_self_hosted_runners_are_named():
    matter = onboarding.classify(GITHUB.format("matter-engine"), _workflows("matter-engine-cpp"))
    assert any("self-hosted" in note for note in matter.workflows[0].notes)


def test_agent_queue_workflows_reproduce_the_reviewed_policy():
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
        check_version="tests-yml-v3",
    )

    assert classification.shape == "github_ci"
    assert parent.playbook_id == "agent-queue-parent-integration"
    assert root.playbook_id == "agent-queue-root-train"
    expected = json.loads((ROOT / "docs/config/agent-queue-train-policy.json").read_text())
    assert plan.policy == expected


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


@pytest.mark.parametrize(
    ("mode", "producer"), [("existing-login", "github-actions"), ("app", "15368")]
)
def test_policy_producer_follows_the_credential_mode(mode, producer):
    parent, root = _shared_routes()
    policy = onboarding.build_policy(
        checks=("Tests",),
        check_version="ci-x",
        producer_id=onboarding.producer_for(mode),
        parent_route=parent,
        root_route=root,
        intelligence_class="standard-high",
        profile_id="standard-high-codex",
    )
    validated = HierarchicalIntegrationPolicy.model_validate(policy)
    assert validated.root.required_checks.producer_id == producer
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
            profile_id="p",
        )


def test_check_set_version_moves_with_the_names():
    assert onboarding.check_set_version(["a", "b"]) == onboarding.check_set_version(("a", "b"))
    assert onboarding.check_set_version(["a", "b"]) != onboarding.check_set_version(["b", "a"])


def test_app_mode_plan_carries_a_valid_trust_manifest_and_variables():
    classification = onboarding.classify(GITHUB.format("outrider-ide"), _workflows("outrider-ide"))
    parent, root = _shared_routes()
    facts = onboarding.ProjectFacts("outrider-ide", GITHUB.format("outrider-ide"))
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


def test_trigger_missing_plan_starts_with_the_workflow_change():
    classification = onboarding.classify(GITHUB.format("jackkern.com"), _workflows("jackkern.com"))
    parent, root = _shared_routes()
    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts("jackkern.com", GITHUB.format("jackkern.com")),
        classification,
        parent_route=parent,
        root_route=root,
    )
    assert plan.steps[0].title == "Make CI run on the train's refs first"
    assert plan.policy is not None


def test_no_ci_plan_stops_at_the_workflow_and_offers_the_interim_path():
    files = _files("quilt-trader")
    classification = onboarding.classify(GITHUB.format("quilt-trader"), {})
    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts("quilt-trader", GITHUB.format("quilt-trader")),
        classification,
        parent_route=None,
        root_route=None,
        validation=onboarding.validation_command(files),
    )
    assert _titles(plan) == [
        "Land a CI workflow first",
        "Optional interim: the development train until CI lands",
    ]
    assert plan.policy is None
    assert '"$venv/bin/pip" install' in plan.steps[1].commands[0]


def test_ci_template_gates_the_train_with_a_tests_job():
    for project in ("quilt-trader", "rom-downloader", "matter-engine-web"):
        template = onboarding.ci_workflow_template(_files(project))
        classification = onboarding.classify(GITHUB.format(project), {"ci.yml": template})
        assert classification.shape == "github_ci"
        assert classification.required.names == ("Tests",)
        assert "${{ secrets." not in template

    assert "exit 1" in onboarding.ci_workflow_template({})
    assert "- run: make check" in onboarding.ci_workflow_template({}, test_command="make check")


def test_local_remote_plan_enters_development_with_a_validation_command():
    files = _files("matter-engine-web") | {"package-lock.json": ""}
    classification = onboarding.classify("/srv/local-remotes/matter-engine-web.git", {})
    validation = onboarding.validation_command(files)
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
        validation=validation,
    )
    assert validation == "npm ci && npm test && npm run build"
    assert plan.path == "development"
    assert _titles(plan) == ["Deliver BLOCKED tasks first", "Enter the development train"]
    assert (
        plan.steps[1]
        .commands[0]
        .startswith(
            "aq integration develop matter-engine-web --validation focused "
            "--command 'npm ci && npm test && npm run build' --interval-seconds 300"
        )
    )

    already = onboarding.plan_onboarding(
        onboarding.ProjectFacts("x", "/srv/x.git", current_mode="development"),
        classification,
        parent_route=None,
        root_route=None,
        validation="true",
    )
    assert _titles(already) == ["Already on the development train"]


def test_pnpm_projects_prefer_their_check_script():
    files = _files("agent-queue-site") | {"pnpm-lock.yaml": ""}
    assert onboarding.validation_command(files) == "pnpm install --frozen-lockfile && pnpm check"


def test_missing_validation_is_a_problem_not_a_guess():
    plan = onboarding.plan_onboarding(
        onboarding.ProjectFacts("x", "/srv/x.git"),
        onboarding.classify("/srv/x.git", {}),
        parent_route=None,
        root_route=None,
    )
    assert "no validation command" in plan.problems[0]
    assert "--command 'VALIDATION'" in plan.steps[-1].commands[0]


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
    assert policy.root.required_checks.producer_id == "github-actions"
    assert policy.root.route.playbook_id == "root-train"
    commands = [command for command, _args in (call.args for call in client.execute.call_args_list)]
    assert set(commands) == {"get_project", "integration_status", "list_tasks"}


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
    assert data["validation_command"] == "npm ci && npm test && npm run build"
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
    client = _client({"get_project": None, "integration_status": None, "list_tasks": None})
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
