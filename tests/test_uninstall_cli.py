"""``aq uninstall`` — what it removes, what it refuses, and what it asks first.

The command's whole value is in the refusals, so most of this file is about
them: a reused PostgreSQL server survives, a shared Homebrew formula survives,
a destructive scope is never taken on an unattended run without ``--yes``, and
declining one prompt narrows the plan instead of aborting the run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.cli.uninstall import selected_scopes
from src.install.lifecycle import RemovalScope
from src.install.postgres import RESOURCE_DATABASE, RESOURCE_ROLE
from src.install.results import EXIT_CODES, InstallOutcome, ResourceRecord
from src.install.state import InstallState, save_state


@pytest.fixture(autouse=True)
def _pg_backend():
    """The uninstaller runs before (and after) a database exists."""


def _cli():
    from src.cli.app import cli

    return cli


def _invoke(*args, **kwargs):
    return CliRunner().invoke(_cli(), ["uninstall", *args], **kwargs)


def _payload(result):
    return json.loads(result.output.strip().splitlines()[-1])


@pytest.fixture
def recorded(tmp_path: Path) -> Path:
    """A resume record shaped like a finished install on this box."""
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    for record in (
        ResourceRecord(kind="config", id=str(tmp_path / "config.yaml"), owned=True),
        ResourceRecord(kind="directory", id=str(tmp_path / "home"), owned=True),
        ResourceRecord(kind=RESOURCE_DATABASE, id="agent_queue", owned=True),
        ResourceRecord(kind=RESOURCE_ROLE, id="agent_queue", owned=True),
        ResourceRecord(kind="postgres-server", id="localhost:5432", owned=False, reused=True),
        ResourceRecord(kind="brew-formula", id="tmux", owned=True),
    ):
        state.resources[record.key] = record
    path = tmp_path / "install-state.json"
    (tmp_path / "home").mkdir()
    (tmp_path / "config.yaml").write_text("messaging_platform: none\n", encoding="utf-8")
    save_state(state, path, now="2026-09-09T00:00:00+00:00")
    return path


def test_uninstall_is_a_top_level_command():
    import click

    cli = _cli()
    assert "uninstall" in cli.list_commands(click.Context(cli))


def test_all_selects_exactly_the_three_flagged_scopes():
    assert selected_scopes(
        remove_config=False, remove_data=False, remove_database=False, everything=True
    ) == frozenset(RemovalScope)
    assert selected_scopes(
        remove_config=False, remove_data=False, remove_database=False, everything=False
    ) == frozenset({RemovalScope.RUNTIME})


def test_no_resume_record_is_a_finished_uninstall_not_a_failure(tmp_path: Path):
    result = _invoke("--json", "--state-file", str(tmp_path / "nothing.json"))
    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["outcome"] == "ready"
    assert "nothing `aq install` recorded owning" in payload["messages"][0]


def test_an_unreadable_record_is_invalid_input(tmp_path: Path):
    path = tmp_path / "install-state.json"
    path.write_text("{not json", encoding="utf-8")
    result = _invoke("--json", "--state-file", str(path))
    assert result.exit_code == EXIT_CODES[InstallOutcome.INVALID_INPUT]


def test_a_dry_run_removes_nothing_and_keeps_the_record(recorded: Path):
    result = _invoke("--all", "--dry-run", "--json", "--state-file", str(recorded))
    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["dry_run"] is True
    assert payload["removed"] == []
    assert recorded.exists()
    assert Path(payload["plan"]["state_path"]).exists()


def test_the_default_plan_keeps_config_data_and_the_database(recorded: Path):
    payload = _payload(_invoke("--dry-run", "--json", "--state-file", str(recorded)))
    planned = {(row["kind"], row["action"]) for row in payload["plan"]["items"]}
    assert ("config", "keep") in planned
    assert ("directory", "keep") in planned
    assert (RESOURCE_DATABASE, "keep") in planned
    assert payload["plan"]["destructive_scopes"] == []


def test_a_reused_server_and_a_shared_formula_are_never_removed(recorded: Path):
    payload = _payload(_invoke("--all", "--dry-run", "--json", "--state-file", str(recorded)))
    kept = {row["kind"] for row in payload["kept"]}
    manual = {row["kind"] for row in payload["manual"]}
    removed = {row["kind"] for row in payload["plan"]["items"] if row["action"] == "remove"}
    assert "postgres-server" in kept
    assert "brew-formula" in manual
    assert not {"postgres-server", "brew-formula"} & removed


def test_an_unattended_destructive_run_without_yes_stops_at_needs_user(recorded: Path):
    result = _invoke("--non-interactive", "--remove-data", "--json", "--state-file", str(recorded))
    assert result.exit_code == EXIT_CODES[InstallOutcome.NEEDS_USER]
    payload = _payload(result)
    assert "--remove-data" in payload["messages"][0]
    assert recorded.exists(), "nothing may be removed before the confirmation lands"


def test_declining_one_prompt_narrows_the_plan_rather_than_aborting(recorded: Path, tmp_path):
    """Answering "no" to the data question still finishes the runtime removal."""
    result = _invoke(
        "--interactive",
        "--remove-config",
        "--remove-data",
        "--json",
        "--state-file",
        str(recorded),
        input="y\nn\n",
    )
    assert result.exit_code == 0
    payload = _payload(result)
    assert not (tmp_path / "config.yaml").exists(), "the confirmed scope ran"
    assert (tmp_path / "home").exists(), "the declined scope did not"
    assert any("data removal was declined" in message for message in payload["messages"])
    assert not recorded.exists(), "the resume record is always the last thing to go"


def test_the_runtime_default_leaves_every_file_in_place(recorded: Path, tmp_path):
    result = _invoke("--non-interactive", "--json", "--state-file", str(recorded))
    assert result.exit_code == 0
    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / "home").exists()
    assert not recorded.exists()


def test_the_human_output_names_what_it_kept_and_what_it_left_for_you(recorded: Path):
    result = _invoke("--all", "--dry-run", "--state-file", str(recorded))
    assert result.exit_code == 0
    assert "Kept" in result.output
    assert "Left for you to remove by hand" in result.output
    assert "brew uninstall tmux" in result.output


# ---------------------------------------------------------------------------
# The PostgreSQL administrator route follows the host the install ran on
# ---------------------------------------------------------------------------


def _state_recorded_on(facts) -> InstallState:
    """A resume record exactly as the engine writes it: facts, not a verdict."""
    state = InstallState(installer_version="0.1.0", target_version="0.1.0")
    state.set_platform(facts)
    return state


def test_a_mac_install_is_uninstalled_through_psql_as_the_invoking_user(tmp_path):
    """Homebrew's cluster superuser is the macOS user; there is no `postgres` account.

    The record holds platform facts with no ``host_path`` key, and reading that
    missing key sent a Mac down the ``sudo -u postgres`` route, leaving the
    database and role the install created behind.
    """
    from src.cli.uninstall import _admin_executor
    from src.install.platform import PlatformFacts

    mac = PlatformFacts(
        system="darwin",
        release="24.6.0",
        machine="arm64",
        arch="arm64",
        python_version="3.12.10",
        macos_version="15.7",
    )
    homebrew_psql = "/opt/homebrew/bin/psql"

    executor, reason = _admin_executor(
        _state_recorded_on(mac),
        home=tmp_path,
        environ={"USER": "jack.kern", "HOME": str(tmp_path)},
        which={"psql": homebrew_psql}.get,
    )

    assert executor is not None, reason
    assert executor.label == "psql as jack.kern"
    assert "sudo" not in executor.label


def test_a_psql_only_in_the_homebrew_prefix_is_still_found_on_a_mac(tmp_path, monkeypatch):
    from src.cli import uninstall
    from src.install import macos
    from src.install.platform import PlatformFacts

    prefix = tmp_path / "homebrew"
    (prefix / "bin").mkdir(parents=True)
    psql = prefix / "bin" / "psql"
    psql.write_text("#!/bin/sh\n", encoding="utf-8")
    psql.chmod(0o755)
    monkeypatch.setattr(macos, "DEFAULT_PREFIXES", {"arm64": prefix})
    mac = PlatformFacts(
        system="darwin", release="24.6.0", machine="arm64", arch="arm64",
        python_version="3.12.10", macos_version="15.7",
    )

    executor, reason = uninstall._admin_executor(
        _state_recorded_on(mac),
        home=tmp_path,
        environ={"USER": "jack.kern"},
        which=lambda name: None,  # the uninstalling shell's PATH has no Homebrew
    )

    assert executor is not None, reason
    assert executor.label == "psql as jack.kern"


def test_a_wsl_install_keeps_the_postgres_system_user_route(tmp_path):
    from src.cli.uninstall import _recorded_host_path
    from src.install.platform import PlatformFacts

    wsl = PlatformFacts(
        system="linux",
        release="6.6.0-microsoft-standard-WSL2",
        machine="x86_64",
        arch="x86_64",
        python_version="3.12.3",
        distro_id="ubuntu",
        distro_version="24.04",
        wsl=True,
        wsl_version=2,
    )

    assert _recorded_host_path(_state_recorded_on(wsl), {}) == "windows-wsl2"
