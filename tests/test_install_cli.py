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
    """Point the installer at a disposable data directory."""
    home = tmp_path / "aq-home"
    monkeypatch.setenv("AQ_INSTALL_STATE_DIR", str(home))
    return home


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


def test_approving_every_step_completes_and_records_state(install_home):
    result = _invoke("--non-interactive", "--yes", "--json")
    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["outcome"] == "ready"
    record = Path(payload["state_path"])
    assert record.exists()
    assert record.stat().st_mode & 0o777 == 0o600
    kinds = {row["kind"] for row in payload["resources"]}
    assert "directory" in kinds


def test_a_rerun_is_safe_and_reports_the_same_single_owned_directory(install_home):
    first = _payload(_invoke("--non-interactive", "--yes", "--json"))
    second = _payload(_invoke("--non-interactive", "--yes", "--json"))
    assert second["outcome"] == "ready"
    assert first["resources"] == second["resources"]
    directories = [row for row in second["resources"] if row["kind"] == "directory"]
    assert len(directories) == 1


# -- interactive ------------------------------------------------------------


def test_declining_a_prompt_skips_the_step_without_failing_the_run(install_home):
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


def test_the_documented_step_states_are_the_ones_the_code_reports():
    from src.install.results import StepState

    text = DOC.read_text(encoding="utf-8")
    for state in StepState:
        assert f"`{state.value}`" in text
