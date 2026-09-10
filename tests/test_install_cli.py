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

from src.cli.install import build_options, load_install_input
from src.install.results import EXIT_CODES, InstallOutcome

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "reference" / "cli" / "install.md"


@pytest.fixture(autouse=True)
def _pg_backend():
    """The installer CLI runs before a database exists."""


@pytest.fixture
def install_home(tmp_path, monkeypatch):
    """Point the installer at a disposable supported host and data directory."""
    from src.cli import install as install_cli
    from src.install.platform import PlatformFacts, SupportVerdict, TIER_SUPPORTED

    home = tmp_path / "aq-home"
    monkeypatch.setenv("AQ_INSTALL_STATE_DIR", str(home))
    monkeypatch.setattr(
        install_cli,
        "describe_host",
        lambda: SupportVerdict(
            host_path="windows-wsl2",
            tier=TIER_SUPPORTED,
            facts=PlatformFacts(
                system="linux",
                release="5.15.0-microsoft-standard-WSL2",
                machine="x86_64",
                arch="x86_64",
                python_version="3.12.0",
                wsl=True,
                wsl_version=2,
                distro_id="ubuntu",
                distro_version="24.04",
            ),
        ),
    )
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
    assert payload["blocking_step"] == "prereq.data-dir"
    assert "--approve prereq.data-dir" in payload["next_action"]


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
def wizard_registry(monkeypatch, tmp_path):
    """A registry with the onboarding steps but no database or platform adapter.

    ``aq install`` composes those adapters for the host it runs on; a test
    about the *wizard* must not depend on whether this box has PostgreSQL
    listening or a Homebrew prefix.
    """
    from src.cli import install as install_cli
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
            )
        )
        return registry

    monkeypatch.setattr(install_cli, "build_registry", build)
    return home


def _questions(*ids_and_capabilities):
    from src.install.wizard import Question

    return tuple(
        Question(id=name, prompt=f"Use {name}?", capability=capability, default=default)
        for name, capability, default in ids_and_capabilities
    )


@pytest.fixture
def scripted_questions(monkeypatch):
    """Replace the machine probes with a fixed question plan."""
    from src.cli import install as install_cli

    plan = _questions(
        ("provider.codex", "provider.codex", False),
        ("daemon", "daemon", False),
    )
    monkeypatch.setattr(install_cli, "wizard_questions", lambda *, advanced: plan)
    return plan


def _plan_action(output: str, step_id: str) -> str:
    """The action the rendered dry-run plan gives *step_id*."""
    match = re.search(rf"^\s+(\S+)\s+{re.escape(step_id)}\s", output, re.MULTILINE)
    assert match, f"{step_id} is missing from the printed plan:\n{output}"
    return match.group(1)


def test_the_wizard_asks_its_questions_and_the_answers_select_capabilities(
    install_home, wizard_registry, scripted_questions
):
    result = _invoke("--interactive", "--dry-run", input="y\nn\n")

    assert "Use provider.codex?" in result.output
    assert "Press Enter to accept each default" in result.output
    # Yes to Codex, no to the daemon: one capability-gated step is planned and
    # the other is recorded as unselected.
    assert _plan_action(result.output, "provider.codex-cli") != "skip_not_selected"
    assert _plan_action(result.output, "daemon.start") == "skip_not_selected"


def test_pressing_enter_through_the_wizard_takes_the_defaults(
    install_home, wizard_registry, scripted_questions
):
    result = _invoke("--interactive", "--dry-run", input="\n\n")

    assert result.exit_code == 0
    # Both defaults were "no", so both capability-gated steps are unselected.
    assert _plan_action(result.output, "provider.codex-cli") == "skip_not_selected"
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


def test_the_advanced_flag_is_passed_to_the_question_plan(
    install_home, wizard_registry, monkeypatch
):
    from src.cli import install as install_cli

    seen: list[bool] = []

    def plan(*, advanced):
        seen.append(advanced)
        return ()

    monkeypatch.setattr(install_cli, "wizard_questions", plan)
    _invoke("--interactive", "--dry-run", "--advanced")

    assert seen == [True]


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
