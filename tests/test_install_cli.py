"""``aq install`` — flags, exit codes, JSON payload and the documented contract.

The exit-code table is API for scripts, so one test reads it back out of
``docs/reference/cli/install.md``: the published table and
``src.install.results.EXIT_CODES`` cannot drift apart silently.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.cli.install import build_options, load_install_input, resolve_project_folder
from src.install.results import EXIT_CODES, InstallOutcome

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "reference" / "cli" / "install.md"


def _bootstrap_checkout(tmp_path, repo, *, aq_on_path=False, **selectors):
    """Execute the bootstrap's Git boundary, without installing machine prerequisites."""
    import os
    import shlex
    import subprocess
    import sys

    script = (ROOT / "scripts" / "install.sh").read_text()
    start = script.index('if [[ -e "$checkout_dir" && ! -d "$checkout_dir/.git" ]]')
    end = script.index('    if [[ ! -x "$checkout_dir/.venv/bin/python" ]]', start)
    checkout = tmp_path / "bootstrap"
    state_dir = tmp_path / "state"
    prelude = (
        "set -euo pipefail\n"
        + "\n".join(
            f"{name}={shlex.quote(str(value))}"
            for name, value in {
                "checkout_dir": checkout,
                "repository": f"file://{repo.origin}",
                "python_bin": sys.executable,
            }.items()
        )
        + (
            '\ncommand() { if [[ "$*" == "-v aq" ]]; then echo /existing/aq; return 0; fi; '
            'builtin command "$@"; }\n'
            if aq_on_path
            else '\ncommand() { if [[ "$*" == "-v aq" ]]; then return 1; fi; builtin command "$@"; }\n'
        )
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"AQ_REF", "AQ_TAG_GLOB", "AQ_PROMOTION_TARGET"}
    }
    env.update(AQ_INSTALL_STATE_DIR=str(state_dir), **selectors)
    result = subprocess.run(
        ["bash"],
        input=prelude + script[start:end] + "fi\n",
        capture_output=True,
        text=True,
        env=env,
    )
    return result, checkout, state_dir


def test_bootstrap_pins_an_explicit_annotated_release_and_records_its_sha(tmp_path):
    from tests.test_update import Repo, _git, _release

    repo = Repo(tmp_path)
    expected = repo.push("README.md", "release\n")
    _release(repo, "v0.2.0")
    repo.push("README.md", "unreleased main\n")
    result, checkout, state_dir = _bootstrap_checkout(tmp_path, repo, AQ_REF="v0.2.0")
    assert result.returncode == 0, result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == expected
    assert _git(checkout, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    record = json.loads((state_dir / "deploy.json").read_text())
    assert record["tag"] == "v0.2.0" and record["commit"] == expected


def test_bootstrap_without_a_selector_keeps_main_head(tmp_path):
    from tests.test_update import Repo, _git, _release

    repo = Repo(tmp_path)
    _release(repo, "v0.2.0")
    expected = repo.push("README.md", "main ahead\n")
    result, checkout, state_dir = _bootstrap_checkout(tmp_path, repo)
    assert result.returncode == 0, result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == expected
    assert _git(checkout, "branch", "--show-current") == "main"
    assert not (state_dir / "deploy.json").exists()


def test_bootstrap_honors_tag_selection_when_aq_is_already_on_path(tmp_path):
    from tests.test_update import Repo, _git, _release

    repo = Repo(tmp_path)
    expected = repo.head()
    _release(repo, "v0.2.0")
    repo.push("README.md", "unreleased main\n")
    result, checkout, state_dir = _bootstrap_checkout(
        tmp_path, repo, aq_on_path=True, AQ_REF="v0.2.0"
    )
    assert result.returncode == 0, result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == expected
    assert json.loads((state_dir / "deploy.json").read_text())["tag"] == "v0.2.0"


def test_bootstrap_without_selection_preserves_an_existing_aq_on_path(tmp_path):
    from tests.test_update import Repo

    repo = Repo(tmp_path)
    result, checkout, state_dir = _bootstrap_checkout(tmp_path, repo, aq_on_path=True)
    assert result.returncode == 0, result.stderr
    assert "Using the aq already on PATH" in result.stdout
    assert not checkout.exists() and not (state_dir / "deploy.json").exists()


def test_bootstrap_resolves_newest_semver_and_falls_back_before_first_release(tmp_path):
    from tests.test_update import Repo, _git, _release

    repo = Repo(tmp_path)
    _release(repo, "v0.9.0")
    expected = repo.push("README.md", "release\n")
    _release(repo, "v0.10.0")
    result, checkout, _ = _bootstrap_checkout(tmp_path, repo, AQ_TAG_GLOB="v*")
    assert result.returncode == 0, result.stderr
    assert _git(checkout, "rev-parse", "HEAD") == expected

    fallback = tmp_path / "fallback"
    fallback.mkdir()
    result, checkout, state_dir = _bootstrap_checkout(fallback, repo, AQ_TAG_GLOB="future-v*")
    assert result.returncode == 0, result.stderr
    assert "unreleased install" in result.stderr
    assert _git(checkout, "branch", "--show-current") == "main"
    assert not (state_dir / "deploy.json").exists()


@pytest.mark.parametrize("ref,lightweight", [("v9.9.9", False), ("v0.2.0", True)])
def test_bootstrap_refuses_missing_or_lightweight_tags(tmp_path, ref, lightweight):
    from tests.test_update import Repo, _release

    repo = Repo(tmp_path)
    if lightweight:
        _release(repo, ref, annotated=False)
    result, checkout, _ = _bootstrap_checkout(tmp_path, repo, AQ_REF=ref)
    assert result.returncode == 20
    assert not checkout.exists()


@pytest.mark.parametrize("matching", [False, True])
def test_bootstrap_reinstall_reconciles_deploy_record(tmp_path, matching):
    from tests.test_update import Repo, _release

    repo = Repo(tmp_path)
    _release(repo, "v0.1.0")
    result, _, state_dir = _bootstrap_checkout(tmp_path, repo, AQ_REF="v0.1.0")
    assert result.returncode == 0, result.stderr
    path = state_dir / "deploy.json"
    previous = json.loads(path.read_text())
    previous["previous_commit"] = "retained-rollback-commit"
    path.write_text(json.dumps(previous))
    before = path.read_bytes()
    updater = tmp_path / "bootstrap" / ".venv" / "bin" / "aq"
    updater.parent.mkdir(parents=True)
    # The Git-boundary fixture omits installer setup and the updater. Model its
    # successful preflight; the bootstrap still resolves and pins the real tag.
    updater.write_text("#!/bin/sh\nexit 0\n")
    updater.chmod(0o755)
    if not matching:
        next_commit = repo.push("README.md", "new release\n")
        _release(repo, "v0.2.0")
    result, _, _ = _bootstrap_checkout(tmp_path, repo, AQ_REF="v0.1.0" if matching else "v0.2.0")
    assert result.returncode == 0, result.stderr
    record = json.loads(path.read_text())
    if matching:
        assert path.read_bytes() == before
    else:
        assert record["commit"] == next_commit and record["selector"] == "v0.2.0"
        assert record["previous_commit"] == previous["commit"]


def test_bootstrap_branch_reinstall_reconciles_stale_release_receipt(tmp_path):
    from tests.test_update import Repo

    repo = Repo(tmp_path)
    result, _, state_dir = _bootstrap_checkout(tmp_path, repo)
    assert result.returncode == 0, result.stderr
    previous = repo.head()
    path = state_dir / "deploy.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(commit=previous, selector="v0.1.0", kind="release")))
    expected = repo.push("README.md", "branch reinstall\n")
    result, _, _ = _bootstrap_checkout(tmp_path, repo)
    assert result.returncode == 0, result.stderr
    record = json.loads(path.read_text())
    assert record["commit"] == expected and record["selector"] == "main"
    assert record["kind"] == "unreleased" and record["tag"] is None
    assert record["previous_commit"] == previous


@pytest.fixture(autouse=True)
def _pg_backend():
    """The installer CLI runs before a database exists."""


@pytest.fixture(autouse=True)
def supported_host(monkeypatch):
    """Pin the host the CLI sees to a supported one.

    ``aq install`` refuses an unsupported host before it plans anything
    (exit 12 ``unsupported_host``), and GitHub's runners are plain Ubuntu,
    not WSL2 -- so a test about a flag, a rerun or the wizard must not
    depend on the machine running it.  Host detection itself is covered in
    ``tests/test_install_platform.py``.
    """
    from src.cli import install as install_cli
    from tests.installer_machine import WSL2

    monkeypatch.setattr(install_cli, "describe_host", lambda: WSL2)


@pytest.fixture(autouse=True)
def quiet_git_identity(monkeypatch):
    """Answer the wizard's commit-identity question with "later", without probing gh.

    Every wizard test below scripts its answers line by line; the identity
    question is exercised on its own (``-- the commit identity --``) with a
    scripted discovery, so no test here ever asks a real ``gh``.
    """
    from types import SimpleNamespace

    from src.cli import install as install_cli

    asked: list[bool] = []
    real = install_cli.ask_git_identity
    monkeypatch.setattr(install_cli, "ask_git_identity", lambda: asked.append(True))
    return SimpleNamespace(asked=asked, real=real)


@pytest.fixture
def install_home(tmp_path, monkeypatch):
    """Point the installer at a disposable data directory."""
    home = tmp_path / "aq-home"
    monkeypatch.setenv("AQ_INSTALL_STATE_DIR", str(home))
    return home


@pytest.fixture
def without_database_steps(monkeypatch):
    """Run the CLI over the engine's own steps only.

    A test about a flag, an exit code or a rerun should not depend on whether
    the box running it happens to have PostgreSQL listening; the database
    adapter's own behaviour is covered in ``tests/test_install_postgres.py``.
    """
    from src.cli import install as install_cli
    from src.install.prerequisites import default_registry

    monkeypatch.setattr(
        install_cli,
        "build_registry",
        lambda _support: default_registry(adapters=()),
    )


def _cli():
    from src.cli.app import cli

    return cli


def _invoke(*args, **kwargs):
    return CliRunner().invoke(_cli(), ["install", *args], **kwargs)


def _payload(result):
    return json.loads(result.output.strip().splitlines()[-1])


# -- registration and discovery ---------------------------------------------


def test_install_is_a_top_level_command():
    import click

    cli = _cli()
    assert "install" in cli.list_commands(click.Context(cli))


def test_list_steps_reports_the_registered_steps_and_their_shape():
    result = _invoke("--list-steps", "--json")
    assert result.exit_code == 0
    steps = _payload(result)["steps"]
    ids = [step["id"] for step in steps]
    assert ids[0] == "host.supported"
    assert "prereq.data-dir" in ids
    assert {"provider.claude-cli", "provider.codex-cli", "provider.gemini-cli"} <= set(ids)
    capabilities = {step["capability"] for step in steps}
    assert {"provider.claude", "provider.codex", "provider.gemini"} <= capabilities
    data_dir = next(step for step in steps if step["id"] == "prereq.data-dir")
    assert data_dir["mutating"] is True
    assert data_dir["depends_on"] == ["host.supported"]


# -- dry run ----------------------------------------------------------------


def test_a_dry_run_reports_a_plan_and_writes_no_resume_record(install_home):
    result = _invoke("--dry-run", "--json", "--non-interactive")
    payload = _payload(result)
    assert payload["dry_run"] is True
    assert payload["state_path"] is None
    actions = {row["step_id"]: row["action"] for row in payload["plan"]}
    assert actions["prereq.data-dir"] == "would_run"
    assert not install_home.exists()


# -- unattended -------------------------------------------------------------


def test_an_unattended_run_without_approval_stops_at_needs_user(install_home):
    result = _invoke("--non-interactive", "--json")
    assert result.exit_code == EXIT_CODES[InstallOutcome.NEEDS_USER]
    payload = _payload(result)
    assert payload["outcome"] == "needs_user"
    # Which step that is depends on the host -- WSL installs its apt
    # prerequisites first, macOS its Homebrew ones -- so the claim under test is
    # "the first mutating step, named with the flag that approves it", not an id.
    blocking = payload["blocking_step"]
    first_mutating = next(row["step_id"] for row in payload["plan"] if row["mutating"])
    assert blocking == first_mutating
    assert f"--approve {blocking}" in payload["next_action"]


def test_approving_every_step_completes_and_records_state(install_home, without_database_steps):
    result = _invoke("--non-interactive", "--yes", "--json")
    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["outcome"] == "ready"
    record = Path(payload["state_path"])
    assert record.exists()
    assert record.stat().st_mode & 0o777 == 0o600
    kinds = {row["kind"] for row in payload["resources"]}
    assert "directory" in kinds


def test_a_rerun_is_safe_and_reports_the_same_single_owned_directory(
    install_home, without_database_steps
):
    first = _payload(_invoke("--non-interactive", "--yes", "--json"))
    second = _payload(_invoke("--non-interactive", "--yes", "--json"))
    assert second["outcome"] == "ready"
    assert first["resources"] == second["resources"]
    directories = [row for row in second["resources"] if row["kind"] == "directory"]
    assert len(directories) == 1


# -- interactive ------------------------------------------------------------


def test_declining_a_prompt_skips_the_step_without_failing_the_run(
    install_home, without_database_steps
):
    """A decline is a choice, not an error, and it claims no ownership.

    The resume record itself still lands under the AQ home — that is the
    installer's own bookkeeping, not an installed resource — so the assertion
    that matters is that nothing was *recorded as owned*.
    """
    result = _invoke("--interactive", "--json", input="n\n")
    assert result.exit_code == 0
    payload = _payload(result)
    step = next(row for row in payload["steps"] if row["step_id"] == "prereq.data-dir")
    assert step["state"] == "skipped"
    assert "declined" in step["summary"]
    assert [row for row in payload["resources"] if row["kind"] == "directory"] == []


# -- invalid input ----------------------------------------------------------


def test_an_unknown_restart_target_exits_invalid_input(install_home):
    result = _invoke("--non-interactive", "--restart-from", "prereq.nope")
    assert result.exit_code == EXIT_CODES[InstallOutcome.INVALID_INPUT]
    assert "prereq.nope" in result.output


def test_an_unknown_capability_names_the_available_ones(install_home):
    result = _invoke("--non-interactive", "--with", "dashbord")
    assert result.exit_code == EXIT_CODES[InstallOutcome.INVALID_INPUT]
    assert "dashbord" in result.output


def test_approving_a_step_that_does_not_exist_is_refused(install_home):
    result = _invoke("--non-interactive", "--approve", "prereq.gti")
    assert result.exit_code == EXIT_CODES[InstallOutcome.INVALID_INPUT]
    assert "prereq.gti" in result.output


def test_fresh_and_restart_from_cannot_be_combined(install_home):
    result = _invoke("--non-interactive", "--fresh", "--restart-from", "prereq.python")
    assert result.exit_code == EXIT_CODES[InstallOutcome.INVALID_INPUT]


# -- the unattended input file ----------------------------------------------


def test_the_input_file_supplies_capabilities_approvals_and_settings(tmp_path):
    path = tmp_path / "install.yaml"
    path.write_text(
        "version: 1\napprove: ['prereq.data-dir']\nsettings:\n  foo: bar\n", encoding="utf-8"
    )
    from src.install.prerequisites import default_registry

    options = build_options(
        default_registry(),
        interactive=False,
        dry_run=False,
        resume=True,
        fresh=False,
        restart_from=None,
        capabilities=(),
        approve=(),
        assume_yes=False,
        config_path=path,
        state_path=None,
    )
    assert options.approve == frozenset({"prereq.data-dir"})
    assert options.settings == {"foo": "bar"}


def test_an_unknown_key_in_the_input_file_is_rejected_by_name(tmp_path):
    path = tmp_path / "install.yaml"
    path.write_text("capabilties: [dashboard]\n", encoding="utf-8")
    with pytest.raises(Exception) as error:
        load_install_input(path)
    assert "capabilties" in str(error.value)


def test_a_future_input_file_version_is_rejected(tmp_path):
    path = tmp_path / "install.yaml"
    path.write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(Exception, match="version 2"):
        load_install_input(path)


def test_a_non_mapping_input_file_is_rejected(tmp_path):
    path = tmp_path / "install.yaml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    with pytest.raises(Exception, match="mapping"):
        load_install_input(path)


def test_a_scalar_capabilities_value_is_rejected(tmp_path):
    path = tmp_path / "install.yaml"
    path.write_text("capabilities: dashboard\n", encoding="utf-8")
    with pytest.raises(Exception, match="must be a list"):
        load_install_input(path)


def test_an_empty_input_file_is_valid(tmp_path):
    path = tmp_path / "install.yaml"
    path.write_text("", encoding="utf-8")
    assert load_install_input(path) == {}


# -- the published contract -------------------------------------------------


def test_the_documented_exit_codes_are_the_ones_the_code_uses():
    pattern = re.compile(r"^\| `(\d+)` \| `([a-z_]+)` \|", re.MULTILINE)
    rows = pattern.findall(DOC.read_text(encoding="utf-8"))
    documented = {outcome: int(code) for code, outcome in rows}
    assert documented == {outcome.value: code for outcome, code in EXIT_CODES.items()}


def test_the_documented_plan_actions_are_the_ones_the_code_can_emit():
    from src.install.results import PlanAction

    text = DOC.read_text(encoding="utf-8")
    documented = set(re.findall(r"`(run|revalidate|would_run|skip_\w+|blocked)`", text))
    assert documented == {action.value for action in PlanAction}


def test_the_documented_step_states_are_the_ones_the_code_reports():
    from src.install.results import StepState

    text = DOC.read_text(encoding="utf-8")
    for state in StepState:
        assert f"`{state.value}`" in text


# -- the onboarding wizard ---------------------------------------------------


@pytest.fixture
def graft_host():
    """graft and npm as the graft steps see them: never the real PATH or npm.

    ``path`` is where ``graft`` is (``None``: not installed), ``version`` what
    it reports, ``npm`` where npm is, and ``calls`` every command the steps ran.
    """
    return {"path": "/usr/bin/graft", "version": "0.18.0", "npm": None, "calls": []}


@pytest.fixture
def wizard_registry(monkeypatch, tmp_path, graft_host):
    """A registry with the onboarding and graft steps but no database or platform adapter.

    ``aq install`` composes those adapters for the host it runs on; a test
    about the *wizard* must not depend on whether this box has PostgreSQL
    listening or a Homebrew prefix.
    """
    from src.cli import install as install_cli
    from src.install.command import CommandOutput
    from src.install.graft import graft_steps
    from src.install.onboarding import onboarding_steps
    from src.install.prerequisites import default_registry

    home = tmp_path / "aq-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "messaging_platform: none\n"
        "database:\n  url: postgresql+asyncpg://agent_queue:pw@localhost:5432/agent_queue\n",
        encoding="utf-8",
    )

    def build(_support):
        registry = default_registry(adapters=(), state_dir=home)
        registry.extend(
            onboarding_steps(
                environ={"HOME": str(home)},
                home=home,
                runner=lambda argv, **kwargs: (_ for _ in ()).throw(
                    AssertionError("the daemon must not be started in this test")
                ),
                which=lambda name: f"/usr/bin/{name}",
                probe=lambda url: None,
                dashboard_root=home,
            )
        )

        def which(name):
            return {"graft": graft_host["path"], "npm": graft_host["npm"]}.get(
                name, f"/usr/bin/{name}"
            )

        def run(argv, **kwargs):
            argv = tuple(argv)
            graft_host["calls"].append(argv)
            if argv[1:] == ("--version",):
                return CommandOutput(argv, 0, graft_host["version"] + "\n")
            if argv[1:] == ("telemetry", "disable"):
                state = home / ".graft" / "telemetry.json"
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_text('{"enabled": false}', encoding="utf-8")
                return CommandOutput(argv, 0, "telemetry: off.")
            raise AssertionError(f"unexpected command: {argv}")

        registry.extend(
            graft_steps(
                which=which,
                runner=run,
                list_workspaces=list,
                vault=home / "vault",
                home=home,
            )
        )
        return registry

    monkeypatch.setattr(install_cli, "build_registry", build)
    return home


def _questions(*ids_and_capabilities, advanced=()):
    from src.install.wizard import Question

    return tuple(
        Question(
            id=name,
            prompt=f"Use {name}?",
            capability=capability,
            default=default,
            advanced=name in advanced,
        )
        for name, capability, default in ids_and_capabilities
    )


@pytest.fixture
def scripted_questions(monkeypatch, tmp_path):
    """Replace the machine probes with a fixed question plan and projects folder."""
    from src.cli import install as install_cli

    plan = _questions(
        ("provider.codex", "provider.codex", True),
        ("daemon", "daemon", False),
    )
    monkeypatch.setattr(install_cli, "wizard_questions", lambda: plan)
    monkeypatch.setattr(install_cli, "_reserved_folders", lambda: ())
    monkeypatch.chdir(tmp_path)
    return plan


def _plan_action(output: str, step_id: str) -> str:
    """The action the rendered dry-run plan gives *step_id*."""
    match = re.search(rf"^\s+(\S+)\s+{re.escape(step_id)}\s", output, re.MULTILINE)
    assert match, f"{step_id} is missing from the printed plan:\n{output}"
    return match.group(1)


def test_the_wizard_asks_its_questions_and_the_answers_select_capabilities(
    install_home, wizard_registry, scripted_questions
):
    # Two answers, then Enter for the projects folder; a dry run is not confirmed.
    result = _invoke("--interactive", "--dry-run", input="y\nn\n\n")

    assert "Use provider.codex?" in result.output
    assert "Press Enter to accept each default" in result.output
    # Yes to Codex, no to the daemon: one capability-gated step is planned and
    # the other is recorded as unselected.
    assert _plan_action(result.output, "provider.codex-cli") != "skip_not_selected"
    assert _plan_action(result.output, "daemon.start") == "skip_not_selected"


def test_pressing_enter_through_the_wizard_takes_the_defaults(
    install_home, wizard_registry, scripted_questions
):
    result = _invoke("--interactive", "--dry-run", input="\n\n\n")

    # Codex defaults to yes and the daemon to no.  (The dry run's exit code is
    # about whether *this* box has Codex signed in, which is not the question.)
    assert _plan_action(result.output, "provider.codex-cli") != "skip_not_selected"
    assert _plan_action(result.output, "daemon.start") == "skip_not_selected"


def test_yes_takes_the_defaults_without_asking(install_home, wizard_registry, scripted_questions):
    result = _invoke("--interactive", "--dry-run", "--yes")

    assert "Use provider.codex?" not in result.output


def test_an_explicit_capability_flag_suppresses_the_questions(
    install_home, wizard_registry, scripted_questions
):
    result = _invoke("--interactive", "--dry-run", "--with", "provider.codex")

    assert "Use provider.codex?" not in result.output


def test_the_human_summary_says_where_data_lives_and_how_to_open_the_dashboard(
    install_home, wizard_registry
):
    result = _invoke("--non-interactive", "--yes")

    assert "Where AQ stores your data" in result.output
    assert "Dashboard" in result.output
    assert "Next" in result.output


def test_a_rerun_prints_the_same_summary_as_the_first_install(install_home, wizard_registry):
    """The reported defect: the second run's summary sections were empty.

    ``render_summary`` prints no heading when there is nothing under it, so a
    rerun whose ``config.check`` reported only "still satisfied" silently lost
    the whole "Where AQ stores your data" section.
    """
    first = _invoke("--non-interactive", "--yes")
    second = _invoke("--non-interactive", "--yes")

    assert second.exit_code == first.exit_code
    assert "Where AQ stores your data" in second.output
    assert "Dashboard" in second.output


def test_a_rerun_carries_the_locations_into_the_machine_readable_summary(
    install_home, wizard_registry
):
    _invoke("--non-interactive", "--yes", "--json")
    payload = _payload(_invoke("--non-interactive", "--yes", "--json"))

    actions = {row["step_id"]: row["action"] for row in payload["plan"]}
    assert actions["config.check"] == "revalidate"
    labels = {entry["label"] for entry in payload["onboarding"]["locations"]}
    assert {"Configuration", "Vault", "Worktrees"} <= labels


def test_a_repair_reports_the_same_locations_a_first_install_did(install_home, wizard_registry):
    first = _payload(_invoke("--non-interactive", "--yes", "--json"))
    repaired = _payload(_invoke("--non-interactive", "--yes", "--repair", "--json"))

    assert repaired["onboarding"]["locations"] == first["onboarding"]["locations"]
    assert repaired["onboarding"]["locations"] != []


def test_the_machine_readable_result_carries_the_same_summary(install_home, wizard_registry):
    result = _invoke("--non-interactive", "--yes", "--json")

    payload = _payload(result)
    summary = payload["onboarding"]
    assert summary["ready"] is (payload["outcome"] == "ready")
    labels = {entry["label"] for entry in summary["locations"]}
    assert {"Configuration", "Vault", "Worktrees"} <= labels
    assert summary["dashboard"] is not None
    assert isinstance(summary["next_steps"], list)


def test_skipping_discord_leaves_the_run_ready(install_home, wizard_registry):
    result = _invoke("--non-interactive", "--yes", "--json")

    payload = _payload(result)
    assert payload["outcome"] == "ready"
    discord = next(row for row in payload["steps"] if row["step_id"] == "config.discord")
    assert discord["state"] == "skipped"
    assert any("--with discord" in line for line in payload["onboarding"]["skipped"])


@pytest.mark.parametrize("as_json", [False, True])
def test_graft_not_selected_is_a_choice_to_add_later_in_human_and_json_output(
    install_home, wizard_registry, graft_host, as_json
):
    args = ("--json",) if as_json else ()
    result = _invoke("--non-interactive", "--yes", *args)

    assert result.exit_code == 0
    assert graft_host["calls"] == []
    if as_json:
        payload = _payload(result)
        assert payload["outcome"] == "ready"
        states = {row["step_id"]: row["state"] for row in payload["steps"]}
        assert states["graft.cli"] == states["graft.repos"] == "skipped"
        advice = [line for line in payload["onboarding"]["skipped"] if "graft" in line]
    else:
        text = " ".join(result.output.split())
        assert "Not installed (optional)" in text
        advice = [text]
    assert len(advice) == 1
    assert "`aq install --with graft`" in advice[0]


def test_with_graft_an_installed_graft_is_reused_and_its_telemetry_turned_off(
    install_home, wizard_registry, graft_host
):
    result = _invoke("--non-interactive", "--yes", "--json", "--with", "graft")

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["outcome"] == "ready"
    cli_step = next(row for row in payload["steps"] if row["step_id"] == "graft.cli")
    assert cli_step["state"] == "succeeded"
    assert cli_step["detail"]["telemetry"] == "disabled"
    assert cli_step["detail"]["installed"] is False
    repos = next(row for row in payload["steps"] if row["step_id"] == "graft.repos")
    assert repos["state"] == "succeeded"
    assert "no registered project" in repos["summary"]
    # Reused, never reinstalled or upgraded: no npm call at all.
    assert all("npm" not in argv[0] for argv in graft_host["calls"])
    assert payload["onboarding"]["drift"] == []


def test_a_graft_that_cannot_be_installed_never_fails_the_install(
    install_home, wizard_registry, graft_host
):
    graft_host["path"] = None
    result = _invoke("--non-interactive", "--yes", "--with", "graft")

    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "graft.cli" in text and "npm is not on PATH" in text
    assert "ready" in text


def test_an_uncleared_graft_version_is_shown_as_drift(install_home, wizard_registry, graft_host):
    graft_host["version"] = "0.20.0"
    result = _invoke("--non-interactive", "--yes", "--with", "graft")

    assert result.exit_code == 0
    text = " ".join(result.output.split())
    assert "Drift (reported, not changed)" in text
    assert "graft 0.20.0 is outside the 0.18.x series" in text


def test_an_ordinary_run_decides_the_advanced_choices_instead_of_asking(
    install_home, wizard_registry, monkeypatch, tmp_path
):
    """Only the choices a person must make are asked; the rest take their default."""
    from src.cli import install as install_cli

    plan = _questions(
        ("provider.codex", "provider.codex", True),
        ("daemon", "daemon", False),
        advanced=("daemon",),
    )
    monkeypatch.setattr(install_cli, "wizard_questions", lambda: plan)
    monkeypatch.setattr(install_cli, "_reserved_folders", lambda: ())
    monkeypatch.chdir(tmp_path)

    ordinary = _invoke("--interactive", "--dry-run", input="\n\n")
    advanced = _invoke("--interactive", "--dry-run", "--advanced", input="\ny\n\n")

    assert "Use daemon?" not in ordinary.output
    assert _plan_action(ordinary.output, "daemon.start") == "skip_not_selected"
    assert "Use daemon?" in advanced.output
    assert _plan_action(advanced.output, "daemon.start") != "skip_not_selected"


def test_the_plan_is_summarised_and_declining_it_changes_nothing(
    install_home, wizard_registry, scripted_questions
):
    result = _invoke("--interactive", input="\n\n\nn\n")

    assert result.exit_code == 0, result.output
    assert "AQ will:" in result.output
    assert "Coding agents: Codex CLI" in result.output
    assert "Go ahead?" in result.output
    assert "Nothing was changed" in result.output
    assert not (install_home / "install-state.json").exists()


def test_an_approved_plan_runs_without_asking_before_each_step(
    install_home, wizard_registry, scripted_questions, tmp_path, monkeypatch
):
    """The first macOS install asked twenty separate "Create …? [Y/n]" questions."""
    from src.cli import install as install_cli
    from src.install.onboarding import onboarding_steps
    from src.install.prerequisites import data_directory_step, host_step
    from src.install.results import StepResult
    from src.install.steps import StepRegistry, StepSpec

    def onboarding_only(_support):
        # A no-op Codex step instead of the real provider and login steps:
        # whether this box has Codex signed in would otherwise stop the run
        # before the projects folder is recorded.
        codex = StepSpec(
            id="provider.codex-cli",
            title="Install or reuse Codex CLI",
            run=lambda context: StepResult.succeeded("provider.codex-cli", "reused"),
            capability="provider.codex",
        )
        registry = StepRegistry((host_step(), data_directory_step(path=install_home), codex))
        registry.extend(
            onboarding_steps(
                environ={"HOME": str(install_home)},
                home=install_home,
                runner=lambda argv, **kwargs: (_ for _ in ()).throw(AssertionError(argv)),
                which=lambda name: f"/usr/bin/{name}",
                probe=lambda url: None,
                dashboard_root=install_home,
            )
        )
        return registry

    monkeypatch.setattr(install_cli, "build_registry", onboarding_only)
    folder = tmp_path / "code"

    result = _invoke("--interactive", input=f"\n\n{folder}\ny\n")

    assert "Go ahead?" in result.output
    assert "if it does not exist? [Y/n]" not in result.output
    assert "Write AQ's default settings" not in result.output
    assert f"Projects folder: {folder}" in result.output
    config = (install_home / "config.yaml").read_text(encoding="utf-8")
    assert "project_roots:" in config and str(folder) in config
    assert folder.is_dir()


def test_a_run_with_no_coding_agent_is_asked_again(
    install_home, wizard_registry, scripted_questions
):
    result = _invoke("--interactive", "--dry-run", input="n\n\ny\n\n\n")

    assert "AQ needs at least one coding agent" in result.output
    assert result.output.count("Use provider.codex?") == 2
    assert _plan_action(result.output, "provider.codex-cli") != "skip_not_selected"


def test_advanced_keeps_asking_before_each_step(
    install_home, wizard_registry, scripted_questions, tmp_path
):
    result = _invoke("--interactive", "--advanced", input=f"\n\n{tmp_path / 'code'}\n" + "n\n" * 20)

    assert "Go ahead?" not in result.output
    assert "[Y/n]" in result.output.split("Where do your code projects live?")[1]


def test_a_projects_folder_that_is_the_home_directory_is_refused(
    install_home, wizard_registry, scripted_questions
):
    result = _invoke("--interactive", "--dry-run", input=f"\n\n{Path.home()}\n\n")

    assert "whole home folder is too broad" in result.output


def test_an_existing_project_root_is_not_asked_for_again(
    install_home, wizard_registry, scripted_questions
):
    config = install_home / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "project_roots:\n- id: code\n  label: code\n  path: /srv/code\n",
        encoding="utf-8",
    )

    result = _invoke("--interactive", "--dry-run", input="\n\n")

    assert "Where do your code projects live?" not in result.output


# -- the commit identity ------------------------------------------------------

MONA = ("Mona Lisa Octocat", "mona@example.com")


def _discovery(*suggestions, note=""):
    from src.install.git_identity import Discovery

    return Discovery(
        suggestions=tuple(suggestions),
        gh_status="authenticated" if suggestions else "missing",
        gh_note=note,
    )


def _public_suggestion():
    from src.install.git_identity import LABEL_PUBLIC, Suggestion

    return Suggestion(*MONA, f"GitHub @octocat: {LABEL_PUBLIC}", "gh:octocat")


@pytest.fixture
def ask_identity(monkeypatch, quiet_git_identity):
    """Put the real identity question back, over a scripted discovery.

    Call the fixture with the :class:`~src.install.git_identity.Discovery` the
    probes should report; ``None`` makes any probe fail the test.
    """
    from src.cli import install as install_cli

    monkeypatch.setattr(install_cli, "ask_git_identity", quiet_git_identity.real)

    def script(discovery):
        def probe():
            if discovery is None:
                raise AssertionError("gh must not be probed in this test")
            return discovery

        monkeypatch.setattr(install_cli, "git_identity_discovery", probe)

    return script


@pytest.fixture
def onboarding_registry(install_home, monkeypatch):
    """Host, data directory, a no-op Codex step and the real onboarding steps."""
    from src.cli import install as install_cli
    from src.install import logins as logins_module
    from src.install.onboarding import onboarding_steps
    from src.install.prerequisites import data_directory_step, host_step
    from src.install.results import StepResult
    from src.install.steps import StepRegistry, StepSpec

    def build(_support):
        codex = StepSpec(
            id="provider.codex-cli",
            title="Install or reuse Codex CLI",
            run=lambda context: StepResult.succeeded("provider.codex-cli", "reused"),
            capability="provider.codex",
        )
        registry = StepRegistry((host_step(), data_directory_step(path=install_home), codex))
        registry.extend(
            onboarding_steps(
                environ={"HOME": str(install_home)},
                home=install_home,
                runner=lambda argv, **kwargs: (_ for _ in ()).throw(AssertionError(argv)),
                which=lambda name: f"/usr/bin/{name}",
                probe=lambda url: None,
                dashboard_root=install_home,
            )
        )
        return registry

    monkeypatch.setattr(install_cli, "build_registry", build)
    # The post-run catalog refresh probes provider CLIs; none exist here.
    monkeypatch.setattr(logins_module, "probe_all", lambda **kwargs: ())
    return install_home


def _stored_identity(home):
    import yaml

    raw = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    return raw.get("git_identity")


def test_the_wizard_suggests_the_gh_account_and_enter_saves_it(
    onboarding_registry, scripted_questions, ask_identity, tmp_path
):
    ask_identity(_discovery(_public_suggestion()))
    folder = tmp_path / "code"

    result = _invoke("--interactive", input=f"\n\n{folder}\n\ny\n")

    assert "GitHub @octocat: public profile email" in result.output
    assert "Commit as: Mona Lisa Octocat <mona@example.com>" in result.output
    assert "OK Record the Git commit identity" in result.output
    assert _stored_identity(onboarding_registry) == {
        "name": MONA[0],
        "email": MONA[1],
        "source": "gh:octocat",
    }


def test_the_wizard_saves_an_edited_identity_and_re_asks_an_invalid_email(
    onboarding_registry, scripted_questions, ask_identity, tmp_path
):
    ask_identity(_discovery(_public_suggestion()))
    folder = tmp_path / "code"

    result = _invoke(
        "--interactive",
        input=f"\n\n{folder}\ne\nAda Lovelace\nnot-an-email\nada@example.com\ny\n",
    )

    assert "name@domain" in result.output
    assert result.output.count("Commit email") == 2
    assert _stored_identity(onboarding_registry) == {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "source": "manual",
    }


def test_without_gh_the_wizard_offers_manual_entry_and_enter_skips_it(
    install_home, wizard_registry, scripted_questions, ask_identity
):
    from src.install.git_identity import NOT_CONFIGURED_NOTE

    ask_identity(_discovery(note="gh is not installed"))

    result = _invoke("--interactive", "--dry-run", input="\n\n\n\n")

    assert "gh is not installed" in result.output
    assert "Commit name (Enter to set it up later)" in result.output
    assert f"• {NOT_CONFIGURED_NOTE}" in result.output
    # The closing note is printed through Rich, which wraps it to the console.
    assert f"note: {NOT_CONFIGURED_NOTE}" in " ".join(result.output.split())
    assert _plan_action(result.output, "provider.codex-cli") != "skip_not_selected"


def test_a_configured_identity_is_never_asked_for_again(
    install_home, wizard_registry, scripted_questions, ask_identity
):
    ask_identity(None)
    config = install_home / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "git_identity:\n  name: Grace Hopper\n  email: grace@example.com\n",
        encoding="utf-8",
    )
    before = config.read_bytes()

    result = _invoke("--interactive", "--dry-run", input="\n\n\n")

    assert "commits in your projects use this name" not in result.output
    assert "looking up your GitHub account" not in result.output
    assert not isinstance(result.exception, AssertionError), result.exception
    assert config.read_bytes() == before


@pytest.mark.parametrize("mode", ["--repair", "--upgrade"])
def test_repair_and_upgrade_never_ask_for_an_identity(
    install_home, wizard_registry, scripted_questions, ask_identity, mode
):
    ask_identity(None)

    result = _invoke("--interactive", mode, "--dry-run", input="")

    assert "commits in your projects use this name" not in result.output
    assert "looking up your GitHub account" not in result.output
    assert not isinstance(result.exception, AssertionError), result.exception


def test_explicit_flags_answer_the_wizards_identity_question(
    install_home, wizard_registry, scripted_questions, quiet_git_identity
):
    _invoke(
        "--interactive",
        "--dry-run",
        "--git-name",
        "Ada Lovelace",
        "--git-email",
        "ada@example.com",
        input="\n\n\n",
    )

    assert quiet_git_identity.asked == []


def test_unattended_flags_record_the_identity(onboarding_registry, quiet_git_identity):
    result = _invoke(
        "--non-interactive",
        "--yes",
        "--json",
        "--git-name",
        "Ada Lovelace",
        "--git-email",
        "ada@example.com",
    )

    payload = _payload(result)
    row = next(row for row in payload["steps"] if row["step_id"] == "config.git-identity")
    assert row["state"] == "succeeded"
    assert _stored_identity(onboarding_registry) == {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "source": "manual",
    }
    assert quiet_git_identity.asked == []


def test_an_unattended_run_with_nothing_explicit_never_guesses_or_prompts(
    onboarding_registry, ask_identity
):
    from src.install.git_identity import NOT_CONFIGURED_NOTE

    ask_identity(None)

    result = _invoke("--non-interactive", "--yes", "--json", input="")

    payload = _payload(result)
    assert NOT_CONFIGURED_NOTE in payload["messages"]
    assert _stored_identity(onboarding_registry) is None


def test_the_input_file_supplies_the_identity(onboarding_registry, tmp_path):
    path = tmp_path / "install.yaml"
    path.write_text(
        "settings:\n  git_identity:\n    name: Ada Lovelace\n    email: ada@example.com\n",
        encoding="utf-8",
    )

    _invoke("--config", str(path), "--yes", "--json")

    assert _stored_identity(onboarding_registry)["email"] == "ada@example.com"


def test_explicit_flags_replace_a_configured_identity(onboarding_registry):
    _invoke("--non-interactive", "--yes", "--json", "--git-name", "Grace", "--git-email", "g@x.io")

    _invoke("--non-interactive", "--yes", "--json", "--git-name", "Ada", "--git-email", "a@x.io")

    assert _stored_identity(onboarding_registry) == {
        "name": "Ada",
        "email": "a@x.io",
        "source": "manual",
    }


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (("--git-name", "Ada", "--git-email", "not an email"), "invalid Git identity"),
        (("--git-name", "Ada"), "must be given together"),
        (("--git-email", "ada@example.com"), "must be given together"),
        (("--git-name", "", "--git-email", ""), "must not be empty"),
    ],
)
def test_an_unusable_identity_flag_is_invalid_input(install_home, args, fragment):
    result = _invoke("--non-interactive", "--yes", *args)

    assert result.exit_code == EXIT_CODES[InstallOutcome.INVALID_INPUT]
    assert fragment in result.output
    assert not (install_home / "install-state.json").exists()


def test_a_misspelled_identity_setting_is_invalid_input(install_home, tmp_path):
    path = tmp_path / "install.yaml"
    path.write_text(
        "settings:\n  git_identity:\n    name: Ada\n    emial: ada@example.com\n",
        encoding="utf-8",
    )

    result = _invoke("--config", str(path), "--yes")

    assert result.exit_code == EXIT_CODES[InstallOutcome.INVALID_INPUT]
    assert "emial" in result.output


def test_identity_flags_win_over_the_input_file(tmp_path):
    from src.install import build_registry
    from tests.installer_machine import WSL2

    path = tmp_path / "install.yaml"
    path.write_text(
        "settings:\n  git_identity:\n    name: Grace\n    email: g@x.io\n    source: gh:g\n",
        encoding="utf-8",
    )
    options = build_options(
        build_registry(WSL2),
        interactive=False,
        dry_run=True,
        resume=True,
        fresh=False,
        restart_from=None,
        capabilities=(),
        approve=(),
        assume_yes=False,
        config_path=path,
        state_path=None,
        git_name="Ada",
        git_email="a@x.io",
    )

    assert options.settings["git_identity"] == {
        "name": "Ada",
        "email": "a@x.io",
        "source": "manual",
    }


# -- repair and upgrade -----------------------------------------------------


def test_repair_and_upgrade_are_mutually_exclusive(install_home, without_database_steps):
    result = _invoke("--repair", "--upgrade", "--non-interactive")
    assert result.exit_code == EXIT_CODES[InstallOutcome.INVALID_INPUT]
    assert "mutually exclusive" in result.output


def test_repair_refuses_to_discard_the_record_it_reconciles(install_home, without_database_steps):
    result = _invoke("--repair", "--fresh", "--non-interactive")
    assert result.exit_code == EXIT_CODES[InstallOutcome.INVALID_INPUT]
    assert "--fresh and --repair are mutually exclusive" in result.output


def test_a_repair_reruns_and_still_owns_exactly_one_directory(install_home, without_database_steps):
    first = _payload(_invoke("--non-interactive", "--yes", "--json"))
    repaired = _payload(_invoke("--repair", "--non-interactive", "--yes", "--json"))

    assert repaired["outcome"] == "ready"
    assert first["resources"] == repaired["resources"]
    assert len([row for row in repaired["resources"] if row["kind"] == "directory"]) == 1


def test_an_upgrade_records_and_completes_a_version_transition(
    install_home, without_database_steps, monkeypatch
):
    from src.cli import install as install_cli

    monkeypatch.setattr(install_cli, "installer_version", lambda: "1.0.0")
    _invoke("--non-interactive", "--yes", "--json")

    monkeypatch.setattr(install_cli, "installer_version", lambda: "1.1.0")
    payload = _payload(_invoke("--upgrade", "--non-interactive", "--yes", "--json"))

    assert payload["outcome"] == "ready"
    assert any("upgrading 1.0.0 -> 1.1.0" in message for message in payload["messages"])
    record = json.loads((install_home / "install-state.json").read_text(encoding="utf-8"))
    assert record["upgrade"] == {
        "from_version": "1.0.0",
        "to_version": "1.1.0",
        "state": "completed",
        "started_at": record["upgrade"]["started_at"],
        "finished_at": record["upgrade"]["finished_at"],
        "attempts": 1,
    }


def test_a_plain_rerun_after_a_version_change_still_refuses(
    install_home, without_database_steps, monkeypatch
):
    """The refusal is what makes ``--repair``/``--upgrade`` an *explicit* plan."""
    from src.cli import install as install_cli

    monkeypatch.setattr(install_cli, "installer_version", lambda: "1.0.0")
    _invoke("--non-interactive", "--yes", "--json")

    monkeypatch.setattr(install_cli, "installer_version", lambda: "1.1.0")
    payload = _payload(_invoke("--non-interactive", "--yes", "--json"))
    assert payload["outcome"] == "invalid_input"
    assert "--restart-from" in payload["next_action"]


def test_a_repair_carries_forward_the_capabilities_the_record_selected(
    install_home, wizard_registry
):
    """A repair reconciles the installation it has, it does not narrow it."""
    selected = _payload(_invoke("--non-interactive", "--yes", "--with", "discord", "--json"))
    assert "discord" in selected["capabilities"]

    repaired = _payload(_invoke("--repair", "--non-interactive", "--yes", "--json"))
    assert repaired["capabilities"] == selected["capabilities"]


def test_a_non_interactive_run_falls_back_to_the_default_projects_folder(tmp_path):
    """Nobody is asked under `curl … | bash`, so the wizard's default stands in.

    Without this the install finished "ready" and then told the operator no
    project root was configured, leaving one manual edit between them and their
    first project -- an extra step the one-line install exists to remove.
    """
    home = tmp_path / "person"
    (home / "Code").mkdir(parents=True)

    folder = resolve_project_folder(None, dry_run=False, cwd=home / "Code", home=home, roots=[])

    assert folder == home / "Code"


def test_an_answer_the_person_gave_is_never_second_guessed(tmp_path):
    chosen = tmp_path / "elsewhere"

    assert resolve_project_folder(chosen, dry_run=False, roots=[]) == chosen
    assert resolve_project_folder(chosen, dry_run=False, roots=["/srv/mine"]) == chosen


def test_a_configured_project_root_is_left_alone(tmp_path):
    home = tmp_path / "person"
    (home / "Code").mkdir(parents=True)

    folder = resolve_project_folder(
        None, dry_run=False, cwd=home / "Code", home=home, roots=["/srv/mine"]
    )

    assert folder is None


def test_a_dry_run_proposes_no_projects_folder_of_its_own(tmp_path):
    """A dry run writes nothing, so it must not print a root it would not add."""
    home = tmp_path / "person"
    (home / "Code").mkdir(parents=True)

    assert (
        resolve_project_folder(None, dry_run=True, cwd=home / "Code", home=home, roots=[]) is None
    )
