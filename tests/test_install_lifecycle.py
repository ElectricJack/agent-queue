"""Repair, upgrade and uninstall: what a machine keeps, and what it loses.

The two acceptance criteria this file exists for are stated as tests rather
than as prose:

* repeated repair and upgrade preserve working customization and data — a
  reconciling rerun does not duplicate a resource, does not narrow the selected
  capabilities and does not delete anything; and
* uninstall removes owned runtime resources without deleting unrelated software
  or databases — a reused server, a shared formula and a provider CLI all
  survive an uninstall that removes the AQ daemon and its own records.

Everything here is pure or driven through injected handlers: nothing in this
file needs a machine to install onto, a daemon to stop or a database to drop.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.install.engine import InstallEngine, InstallOptions
from src.install.lifecycle import (
    DEFAULT_SCOPES,
    KIND_INSTALL_RECORD,
    UPGRADE_COMPLETED,
    UPGRADE_IN_PROGRESS,
    LifecycleMode,
    RemovalAction,
    RemovalItem,
    RemovalOutcome,
    RemovalScope,
    UpgradeRecord,
    complete_upgrade,
    default_handlers,
    execute_uninstall,
    plan_uninstall,
    plan_upgrade,
    strip_marked_block,
)
from src.install.platform import PlatformFacts, SupportVerdict
from src.install.postgres import RESOURCE_CREDENTIAL, RESOURCE_DATABASE, RESOURCE_ROLE, SqlError
from src.install.results import (
    InstallOutcome,
    PlanAction,
    ResourceRecord,
    StepResult,
    StepState,
)
from src.install.state import InstallState, load_state, save_state
from src.install.steps import StepRegistry, StepSpec


@pytest.fixture(autouse=True)
def _pg_backend():
    """The installer lifecycle is pure; it never allocates a database."""


SUPPORTED = SupportVerdict(
    host_path="windows-wsl2",
    tier="supported",
    facts=PlatformFacts(
        system="linux",
        release="6.6.0-microsoft-standard-WSL2",
        machine="x86_64",
        arch="x86_64",
        python_version="3.12.3",
        distro_id="ubuntu",
        distro_version="24.04",
        wsl=True,
        wsl_version=2,
    ),
)


def _clock(value: str = "2026-09-09T00:00:00+00:00"):
    return lambda: value


def _engine(registry: StepRegistry, options: InstallOptions, **kwargs) -> InstallEngine:
    return InstallEngine(
        registry, options, support=SUPPORTED, clock=kwargs.pop("clock", _clock()), **kwargs
    )


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def _counting_registry(counter: dict[str, int], *, verify=None) -> StepRegistry:
    def run(context):
        counter["runs"] = counter.get("runs", 0) + 1
        return StepResult.succeeded(
            "thing",
            "did the thing",
            resources=(ResourceRecord(kind="directory", id="/data", owned=True),),
        )

    return StepRegistry(
        (
            StepSpec(
                id="thing",
                title="Do the thing",
                run=run,
                verify=verify,
                owner="test",
            ),
        )
    )


def test_a_plain_rerun_carries_forward_a_step_that_cannot_be_verified(tmp_path: Path):
    counter: dict[str, int] = {}
    registry = _counting_registry(counter)
    options = InstallOptions(installer_version="1.0.0", state_path=tmp_path / "state.json")

    _engine(registry, options).run()
    assert counter["runs"] == 1

    _engine(registry, options).run()
    assert counter["runs"] == 1, "a plain rerun trusts its record for an unverifiable step"


def test_repair_reruns_a_step_that_cannot_be_verified(tmp_path: Path):
    counter: dict[str, int] = {}
    registry = _counting_registry(counter)
    state_path = tmp_path / "state.json"

    _engine(registry, InstallOptions(installer_version="1.0.0", state_path=state_path)).run()
    assert counter["runs"] == 1

    result = _engine(
        registry,
        InstallOptions(installer_version="1.0.0", state_path=state_path, mode=LifecycleMode.REPAIR),
    ).run()
    assert counter["runs"] == 2
    assert result.outcome is InstallOutcome.READY
    row = next(row for row in result.plan if row.step_id == "thing")
    assert row.action is PlanAction.RUN
    assert "no read-only verifier" in row.reason


def test_repair_does_not_duplicate_an_owned_resource(tmp_path: Path):
    counter: dict[str, int] = {}
    registry = _counting_registry(counter)
    state_path = tmp_path / "state.json"
    _engine(registry, InstallOptions(installer_version="1.0.0", state_path=state_path)).run()

    for _ in range(3):
        result = _engine(
            registry,
            InstallOptions(
                installer_version="1.0.0", state_path=state_path, mode=LifecycleMode.REPAIR
            ),
        ).run()

    assert [(row.kind, row.id) for row in result.resources] == [("directory", "/data")]
    state = load_state(state_path)
    assert list(state.resources) == [("directory", "/data")]


def test_repair_still_revalidates_a_step_that_has_a_verifier(tmp_path: Path):
    counter: dict[str, int] = {}
    registry = _counting_registry(counter, verify=lambda context: True)
    state_path = tmp_path / "state.json"
    _engine(registry, InstallOptions(installer_version="1.0.0", state_path=state_path)).run()

    result = _engine(
        registry,
        InstallOptions(installer_version="1.0.0", state_path=state_path, mode=LifecycleMode.REPAIR),
    ).run()
    assert counter["runs"] == 1, "a satisfied verifier is the cheapest possible repair"
    row = next(row for row in result.plan if row.step_id == "thing")
    assert row.action is PlanAction.REVALIDATE


def test_a_dry_run_repair_never_executes_a_mutating_step(tmp_path: Path):
    counter: dict[str, int] = {}

    def run(context):
        counter["runs"] = counter.get("runs", 0) + 1
        return StepResult.succeeded("thing", "did the thing")

    registry = StepRegistry(
        (StepSpec(id="thing", title="Do it", run=run, mutating=True, owner="test"),)
    )
    state_path = tmp_path / "state.json"
    _engine(
        registry,
        InstallOptions(installer_version="1.0.0", state_path=state_path, approve=frozenset("*")),
    ).run()
    assert counter["runs"] == 1

    result = _engine(
        registry,
        InstallOptions(
            installer_version="1.0.0",
            state_path=state_path,
            mode=LifecycleMode.REPAIR,
            dry_run=True,
            approve=frozenset("*"),
        ),
    ).run()
    assert counter["runs"] == 1
    row = next(row for row in result.plan if row.step_id == "thing")
    assert row.action is PlanAction.WOULD_RUN


def test_a_capability_selected_after_the_run_that_skipped_it_is_installed(tmp_path: Path):
    """Selecting an optional capability on a rerun must actually run its step.

    The published summary tells an operator to "add it with `aq install --with
    …`", which a record that says "previously skipped" would have made a
    permanent no-op.
    """
    counter: dict[str, int] = {}

    def run(context):
        counter["runs"] = counter.get("runs", 0) + 1
        return StepResult.succeeded("optional", "installed the optional thing")

    registry = StepRegistry(
        (
            StepSpec(
                id="optional",
                title="Optional thing",
                run=run,
                capability="extra",
                owner="test",
            ),
        )
    )
    state_path = tmp_path / "state.json"
    first = _engine(
        registry, InstallOptions(installer_version="1.0.0", state_path=state_path)
    ).run()
    assert first.steps[0].state is StepState.SKIPPED
    assert counter.get("runs", 0) == 0

    second = _engine(
        registry,
        InstallOptions(
            installer_version="1.0.0",
            state_path=state_path,
            capabilities=frozenset({"extra"}),
        ),
    ).run()
    assert counter["runs"] == 1
    assert second.steps[0].state is StepState.SUCCEEDED


def test_repair_adopts_a_record_written_by_another_installer_version(tmp_path: Path):
    counter: dict[str, int] = {}
    registry = _counting_registry(counter)
    state_path = tmp_path / "state.json"
    _engine(registry, InstallOptions(installer_version="0.9.0", state_path=state_path)).run()

    refused = _engine(
        registry, InstallOptions(installer_version="1.0.0", state_path=state_path)
    ).run()
    assert refused.outcome is InstallOutcome.INVALID_INPUT

    repaired = _engine(
        registry,
        InstallOptions(installer_version="1.0.0", state_path=state_path, mode=LifecycleMode.REPAIR),
    ).run()
    assert repaired.outcome is InstallOutcome.READY
    assert any("adopting the resume record" in message for message in repaired.messages)
    # Adoption keeps what the older installer owned: forgetting it is what
    # would make the next run create a second copy.
    assert [(row.kind, row.id) for row in repaired.resources] == [("directory", "/data")]
    assert load_state(state_path).installer_version == "1.0.0"


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def test_an_upgrade_records_the_transition_before_the_first_step_runs(tmp_path: Path):
    state_path = tmp_path / "state.json"
    observed: list[str | None] = []

    def run(context):
        # Read the record from disk *during* the run: that is what a process
        # killed halfway would have left behind.
        state = load_state(state_path)
        observed.append(state.upgrade.state if state and state.upgrade else None)
        return StepResult.succeeded("thing", "did the thing")

    registry = StepRegistry((StepSpec(id="thing", title="Do it", run=run, owner="test"),))
    _engine(registry, InstallOptions(installer_version="0.9.0", state_path=state_path)).run()
    assert observed == [None], "a plain install records no version transition"
    observed.clear()

    result = _engine(
        registry,
        InstallOptions(
            installer_version="1.0.0",
            target_version="1.0.0",
            state_path=state_path,
            mode=LifecycleMode.UPGRADE,
        ),
    ).run()
    assert observed == [UPGRADE_IN_PROGRESS]
    assert result.outcome is InstallOutcome.READY
    assert load_state(state_path).upgrade.state == UPGRADE_COMPLETED


def test_an_interrupted_upgrade_is_resumed_by_the_next_run(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    state.upgrade = UpgradeRecord(
        from_version="0.9.0", to_version="1.0.0", started_at="2026-09-08T00:00:00+00:00"
    )
    save_state(state, state_path, now="2026-09-08T00:00:00+00:00")

    registry = StepRegistry(
        (
            StepSpec(
                id="thing",
                title="Do it",
                run=lambda context: StepResult.succeeded("thing", "did it"),
                owner="test",
            ),
        )
    )
    result = _engine(
        registry,
        InstallOptions(installer_version="1.0.0", target_version="1.0.0", state_path=state_path),
    ).run()

    assert any("resuming an interrupted upgrade" in message for message in result.messages)
    finished = load_state(state_path).upgrade
    assert finished.state == UPGRADE_COMPLETED
    assert finished.attempts == 2


def test_an_upgrade_that_does_not_reach_ready_stays_in_progress(tmp_path: Path):
    state_path = tmp_path / "state.json"
    save_state(
        InstallState(installer_version="0.9.0", target_version="0.9.0"),
        state_path,
        now="2026-09-08T00:00:00+00:00",
    )
    registry = StepRegistry(
        (
            StepSpec(
                id="thing",
                title="Do it",
                run=lambda context: StepResult.failed("thing", "it broke", "fix it"),
                owner="test",
            ),
        )
    )
    result = _engine(
        registry,
        InstallOptions(
            installer_version="1.0.0",
            target_version="1.0.0",
            state_path=state_path,
            mode=LifecycleMode.UPGRADE,
        ),
    ).run()
    assert result.outcome is InstallOutcome.FAILED
    assert load_state(state_path).upgrade.state == UPGRADE_IN_PROGRESS


def test_plan_upgrade_supersedes_an_abandoned_target_only_when_asked():
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    state.upgrade = UpgradeRecord(from_version="0.9.0", to_version="1.0.0", started_at="t0")

    passive = plan_upgrade(
        state,
        mode=LifecycleMode.INSTALL,
        from_version="1.0.0",
        target_version="1.1.0",
        now="t1",
    )
    assert passive.record is state.upgrade
    assert not passive.started
    assert "still recorded" in passive.messages[0]

    active = plan_upgrade(
        state,
        mode=LifecycleMode.UPGRADE,
        from_version="1.0.0",
        target_version="1.1.0",
        now="t1",
    )
    assert active.started
    assert active.superseded is state.upgrade
    assert active.record.to_version == "1.1.0"


def test_complete_upgrade_is_idempotent():
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    state.upgrade = UpgradeRecord(from_version="0.9.0", to_version="1.0.0", started_at="t0")
    assert complete_upgrade(state, now="t1").state == UPGRADE_COMPLETED
    assert complete_upgrade(state, now="t2") is None
    assert state.upgrade.finished_at == "t1"


def test_the_upgrade_record_survives_a_save_and_load(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    state.upgrade = UpgradeRecord(from_version="0.9.0", to_version="1.0.0", started_at="t0")
    save_state(state, state_path, now="t0")

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["upgrade"]["from_version"] == "0.9.0"
    assert load_state(state_path).upgrade.to_version == "1.0.0"


# ---------------------------------------------------------------------------
# Uninstall planning
# ---------------------------------------------------------------------------


def _installed_state() -> InstallState:
    """A record shaped like a real install: some owned, some reused, some shared."""
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    for record in (
        ResourceRecord(kind="daemon", id="http://127.0.0.1:8081/api", owned=True),
        ResourceRecord(kind="config", id="/home/you/.agent-queue/config.yaml", owned=True),
        ResourceRecord(kind="directory", id="/home/you/.agent-queue", owned=True),
        ResourceRecord(kind=RESOURCE_DATABASE, id="agent_queue", owned=True),
        ResourceRecord(kind=RESOURCE_ROLE, id="agent_queue", owned=True),
        # Reused: found on the host, never AQ's to remove.
        ResourceRecord(kind="postgres-server", id="localhost:5432", owned=False, reused=True),
        ResourceRecord(kind="command", id="git", owned=False, reused=True),
        # Owned but shared with the rest of the machine.
        ResourceRecord(kind="brew-formula", id="tmux", owned=True),
        ResourceRecord(kind="provider-cli", id="claude", owned=True),
        # Never removed, whatever is selected.
        ResourceRecord(kind=RESOURCE_CREDENTIAL, id="AQ_DB_PASSWORD", owned=True),
    ):
        state.resources[record.key] = record
    return state


def _action(plan, kind: str, resource_id: str) -> RemovalItem:
    return next(item for item in plan.items if item.kind == kind and item.id == resource_id)


def test_the_default_uninstall_removes_the_runtime_and_nothing_else():
    plan = plan_uninstall(_installed_state(), state_path="/home/you/.agent-queue/state.json")

    assert {(item.kind, item.id) for item in plan.removals} == {
        ("daemon", "http://127.0.0.1:8081/api"),
        (KIND_INSTALL_RECORD, "/home/you/.agent-queue/state.json"),
    }
    assert plan.destructive_scopes == ()
    for kind, resource_id in (
        ("config", "/home/you/.agent-queue/config.yaml"),
        ("directory", "/home/you/.agent-queue"),
        (RESOURCE_DATABASE, "agent_queue"),
        (RESOURCE_ROLE, "agent_queue"),
    ):
        item = _action(plan, kind, resource_id)
        assert item.action is RemovalAction.KEEP
        assert "--remove-" in item.reason


def test_uninstall_never_removes_a_resource_the_installer_reused():
    plan = plan_uninstall(
        _installed_state(),
        scopes=frozenset(RemovalScope),
    )
    for kind, resource_id in (("postgres-server", "localhost:5432"), ("command", "git")):
        item = _action(plan, kind, resource_id)
        assert item.action is RemovalAction.KEEP
        assert "reused" in item.reason


def test_uninstall_reports_shared_software_instead_of_removing_it():
    plan = plan_uninstall(_installed_state(), scopes=frozenset(RemovalScope))
    manual = {(item.kind, item.id): item for item in plan.manual}
    assert set(manual) == {("brew-formula", "tmux"), ("provider-cli", "claude")}
    assert "brew uninstall tmux" in manual[("brew-formula", "tmux")].hint
    assert not any(item.kind in {"brew-formula", "provider-cli"} for item in plan.removals)


def test_uninstall_never_touches_a_credential():
    plan = plan_uninstall(_installed_state(), scopes=frozenset(RemovalScope))
    item = _action(plan, RESOURCE_CREDENTIAL, "AQ_DB_PASSWORD")
    assert item.action is RemovalAction.KEEP
    assert "never creates, reads, moves or deletes a credential" in item.reason


def test_an_unknown_resource_kind_is_kept_rather_than_guessed():
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    record = ResourceRecord(kind="something-new", id="x", owned=True)
    state.resources[record.key] = record

    item = plan_uninstall(state, scopes=frozenset(RemovalScope)).items[0]
    assert item.action is RemovalAction.KEEP
    assert "no removal is defined" in item.reason


def test_selecting_a_destructive_scope_with_nothing_owned_confirms_nothing():
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    record = ResourceRecord(kind=RESOURCE_DATABASE, id="agent_queue", owned=False, reused=True)
    state.resources[record.key] = record

    plan = plan_uninstall(state, scopes=DEFAULT_SCOPES | {RemovalScope.DATABASE})
    assert plan.destructive_scopes == ()


def test_removals_run_in_an_order_that_does_not_saw_off_the_branch():
    plan = plan_uninstall(
        _installed_state(),
        scopes=frozenset(RemovalScope),
        state_path="/home/you/.agent-queue/state.json",
    )
    kinds = [item.kind for item in plan.removals]
    assert kinds.index("daemon") < kinds.index(RESOURCE_DATABASE)
    assert kinds.index("config") < kinds.index("directory")
    assert kinds[-1] == KIND_INSTALL_RECORD


# ---------------------------------------------------------------------------
# Uninstall execution
# ---------------------------------------------------------------------------


def test_a_failing_removal_does_not_abandon_the_rest_of_the_plan():
    plan = plan_uninstall(_installed_state(), state_path="/tmp/state.json")
    calls: list[str] = []

    def failing(item):
        calls.append(item.kind)
        raise OSError("device is busy")

    def working(item):
        calls.append(item.kind)
        return RemovalOutcome(item, removed=True, summary="gone")

    result = execute_uninstall(plan, {"daemon": failing, KIND_INSTALL_RECORD: working})
    assert calls == ["daemon", KIND_INSTALL_RECORD]
    assert result.outcome is InstallOutcome.FAILED
    assert "device is busy" in result.failures[0].error


def test_a_dry_run_calls_no_handler():
    plan = plan_uninstall(_installed_state(), state_path="/tmp/state.json")

    def explode(item):  # pragma: no cover - must never be called
        raise AssertionError("a dry run must not touch the host")

    result = execute_uninstall(plan, {"daemon": explode}, dry_run=True)
    assert result.outcome is InstallOutcome.READY
    assert result.outcomes == ()
    assert result.to_dict()["dry_run"] is True


def test_an_unhandled_kind_is_reported_rather_than_silently_skipped():
    plan = plan_uninstall(_installed_state(), state_path="/tmp/state.json")
    result = execute_uninstall(plan, {})
    assert result.outcome is InstallOutcome.FAILED
    assert {row.item.kind for row in result.failures} == {"daemon", KIND_INSTALL_RECORD}


# ---------------------------------------------------------------------------
# The default handlers
# ---------------------------------------------------------------------------


def test_stripping_the_marked_block_leaves_the_rest_of_the_file_alone():
    text = (
        "export EDITOR=vim\n"
        "# >>> agent-queue (aq install) >>>\n"
        'eval "$(/opt/homebrew/bin/brew shellenv)"\n'
        "# <<< agent-queue (aq install) <<<\n"
        "alias ll='ls -l'\n"
    )
    updated, removed = strip_marked_block(
        text, "# >>> agent-queue (aq install) >>>", "# <<< agent-queue (aq install) <<<"
    )
    assert removed
    assert updated == "export EDITOR=vim\nalias ll='ls -l'\n"


def test_stripping_reports_a_file_that_no_longer_holds_the_block():
    updated, removed = strip_marked_block("export EDITOR=vim\n", ">>>", "<<<")
    assert not removed
    assert updated == "export EDITOR=vim\n"


def test_the_shell_profile_handler_removes_only_aqs_block(tmp_path: Path):
    from src.install.macos import BEGIN_MARKER, END_MARKER

    profile = tmp_path / ".zprofile"
    profile.write_text(
        f"export PATH=/usr/local/bin:$PATH\n{BEGIN_MARKER}\nbrew shellenv\n{END_MARKER}\n",
        encoding="utf-8",
    )
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    record = ResourceRecord(
        kind="shell-profile",
        id=str(profile),
        owned=True,
        detail={"marker": BEGIN_MARKER, "marker_end": END_MARKER},
    )
    state.resources[record.key] = record

    plan = plan_uninstall(state)
    result = execute_uninstall(plan, default_handlers(home=tmp_path))
    assert result.outcome is InstallOutcome.READY
    assert profile.read_text(encoding="utf-8") == "export PATH=/usr/local/bin:$PATH\n"


def test_the_directory_handler_refuses_a_path_outside_the_install_home(tmp_path: Path):
    home = tmp_path / "home" / ".agent-queue"
    home.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "important.txt").write_text("keep me", encoding="utf-8")

    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    for path in (home, elsewhere):
        record = ResourceRecord(kind="directory", id=str(path), owned=True)
        state.resources[record.key] = record

    plan = plan_uninstall(state, scopes=DEFAULT_SCOPES | {RemovalScope.DATA})
    result = execute_uninstall(plan, default_handlers(home=home))

    assert result.outcome is InstallOutcome.FAILED
    assert not home.exists()
    assert (elsewhere / "important.txt").read_text(encoding="utf-8") == "keep me"
    assert "refusing to remove" in result.failures[0].error


def test_the_config_handler_keeps_a_copy_of_what_it_removes(tmp_path: Path):
    config = tmp_path / "config.yaml"
    config.write_text("messaging_platform: none\n", encoding="utf-8")
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    record = ResourceRecord(kind="config", id=str(config), owned=True)
    state.resources[record.key] = record

    plan = plan_uninstall(state, scopes=DEFAULT_SCOPES | {RemovalScope.CONFIG})
    result = execute_uninstall(plan, default_handlers(home=tmp_path))

    assert result.outcome is InstallOutcome.READY
    assert not config.exists()
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == (
        "messaging_platform: none\n"
    )


class _RecordingExecutor:
    label = "test"

    def __init__(self, *, fail: bool = False) -> None:
        self.statements: list[str] = []
        self._fail = fail

    def scalar(self, sql: str) -> str | None:  # pragma: no cover - unused here
        return None

    def execute(self, sql: str) -> None:
        self.statements.append(sql)
        if self._fail:
            raise SqlError("permission denied")


def test_dropping_the_database_and_role_uses_the_administrator_connection(tmp_path: Path):
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    for record in (
        ResourceRecord(kind=RESOURCE_DATABASE, id="agent_queue", owned=True),
        ResourceRecord(kind=RESOURCE_ROLE, id="agent_queue", owned=True),
    ):
        state.resources[record.key] = record

    executor = _RecordingExecutor()
    plan = plan_uninstall(state, scopes=DEFAULT_SCOPES | {RemovalScope.DATABASE})
    result = execute_uninstall(plan, default_handlers(home=tmp_path, admin=executor))

    assert result.outcome is InstallOutcome.READY
    assert executor.statements == [
        'DROP DATABASE IF EXISTS "agent_queue"',
        'DROP ROLE IF EXISTS "agent_queue"',
    ]


def test_dropping_without_an_administrator_connection_reports_how_to_finish(tmp_path: Path):
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    record = ResourceRecord(kind=RESOURCE_DATABASE, id="agent_queue", owned=True)
    state.resources[record.key] = record

    plan = plan_uninstall(state, scopes=DEFAULT_SCOPES | {RemovalScope.DATABASE})
    result = execute_uninstall(plan, default_handlers(home=tmp_path, admin=None))

    assert result.outcome is InstallOutcome.FAILED
    assert "administrator connection" in result.failures[0].error


def test_a_recorded_database_name_that_is_not_an_identifier_is_refused(tmp_path: Path):
    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    record = ResourceRecord(kind=RESOURCE_DATABASE, id='aq"; DROP DATABASE other; --', owned=True)
    state.resources[record.key] = record

    executor = _RecordingExecutor()
    plan = plan_uninstall(state, scopes=DEFAULT_SCOPES | {RemovalScope.DATABASE})
    result = execute_uninstall(plan, default_handlers(home=tmp_path, admin=executor))

    assert executor.statements == []
    assert result.outcome is InstallOutcome.FAILED
    assert "not a plain SQL identifier" in result.failures[0].error


def test_stopping_a_daemon_that_is_not_running_is_not_a_failure(tmp_path: Path):
    from src.install.command import CommandOutput

    state = InstallState(installer_version="1.0.0", target_version="1.0.0")
    record = ResourceRecord(kind="daemon", id="http://127.0.0.1:8081/api", owned=True)
    state.resources[record.key] = record

    def runner(argv, **kwargs):
        return CommandOutput(argv=tuple(argv), returncode=1, stderr="no daemon is running")

    plan = plan_uninstall(state)
    result = execute_uninstall(
        plan, default_handlers(home=tmp_path, runner=runner, which=lambda name: "/usr/bin/aq")
    )
    assert result.outcome is InstallOutcome.READY
    assert "was not running" in result.outcomes[0].summary


# ---------------------------------------------------------------------------
# The acceptance criterion, end to end over the real configuration step
# ---------------------------------------------------------------------------


def test_repeated_repair_preserves_a_hand_edited_configuration(tmp_path: Path):
    """Repair reconciles; it does not overwrite an opinion or pile up backups.

    This runs the *real* ``config.defaults`` step rather than a stand-in,
    because "repeated repair preserves working customization" is a claim about
    that step's gap-filling behaviour as much as about the engine's planning.
    """
    from src.install.onboarding import config_step

    home = tmp_path / "aq-home"
    home.mkdir()
    config = home / "config.yaml"
    registry = StepRegistry(
        (config_step(environ={"HOME": str(home)}, home=home, depends_on=()),)
    )
    options = InstallOptions(
        installer_version="1.0.0",
        state_path=tmp_path / "state.json",
        approve=frozenset("*"),
        mode=LifecycleMode.REPAIR,
    )

    assert _engine(registry, options).run().outcome is InstallOutcome.READY
    tuned = config.read_text(encoding="utf-8")

    # An operator edits a value the installer wrote and adds a section it has
    # never heard of.
    config.write_text(tuned + "\nmy_own_section:\n  keep: me\n", encoding="utf-8")

    for _ in range(3):
        assert _engine(registry, options).run().outcome is InstallOutcome.READY

    survived = config.read_text(encoding="utf-8")
    assert "my_own_section" in survived
    assert "keep: me" in survived
    backups = sorted(path.name for path in home.glob("config.yaml.bak*"))
    assert backups == [], "a repair with nothing to add must not leave a copy behind"
