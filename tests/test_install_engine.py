"""The installer engine: ordering, idempotent reruns, resume and secrecy.

The two acceptance criteria this file exists for are stated as tests rather
than as comments: rerunning after a failure must not duplicate a resource, and
a failure must name its step and say what to do next.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.install.engine import InstallEngine, InstallOptions, ProgressEvent
from src.install.platform import PlatformFacts, SupportVerdict
from src.install.prerequisites import (
    STEP_DATA_DIR,
    STEP_GIT,
    STEP_HOST,
    STEP_PYTHON,
    default_registry,
)
from src.install.redaction import (
    SecretLeakError,
    assert_secret_free,
    find_secrets,
    redact,
)
from src.install.results import (
    EXIT_CODES,
    InstallOutcome,
    PlanAction,
    ResourceRecord,
    StepResult,
    StepState,
)
from src.install.state import (
    IncompatibleStateError,
    InstallState,
    StateError,
    load_state,
    save_state,
)
from src.install.steps import InstallPlanError, StepRegistry, StepSpec, merge_resources


@pytest.fixture(autouse=True)
def _pg_backend():
    """The installer engine is pure; it never allocates a database."""


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

UNSUPPORTED = SupportVerdict(
    host_path="unsupported",
    tier="unsupported",
    facts=PlatformFacts(
        system="linux", release="6.8.0", machine="x86_64", arch="x86_64", python_version="3.12.3"
    ),
    reasons=("this Linux host is not a WSL2 distribution",),
    remediation="Install inside WSL2 on Windows, or on macOS 14+.",
)


def _clock():
    ticks = iter(f"2026-09-09T00:00:{index:02d}+00:00" for index in range(60))
    return lambda: next(ticks)


def _options(tmp_path: Path, **overrides) -> InstallOptions:
    defaults = {
        "installer_version": "1.2.3",
        "target_version": "1.2.3",
        "interactive": False,
        "approve": frozenset({"*"}),
        "state_path": tmp_path / "install-state.json",
    }
    defaults.update(overrides)
    return InstallOptions(**defaults)


def _run(registry, tmp_path, **overrides):
    return InstallEngine(
        registry, _options(tmp_path, **overrides), support=SUPPORTED, clock=_clock()
    ).run()


class _Counter:
    """A step body that records how many times it actually executed."""

    def __init__(self, step_id: str, result=None) -> None:
        self.step_id = step_id
        self.calls = 0
        self._result = result

    def __call__(self, context) -> StepResult:
        self.calls += 1
        return self._result or StepResult.succeeded(self.step_id, f"ran {self.calls}x")


def prerequisites_only(**kwargs):
    """The engine's own steps, without the PostgreSQL adapter.

    The database steps have their own suite (``tests/test_install_postgres.py``);
    a test about consent, resume or redaction should not need a server to be
    reachable to say what it is about.
    """
    return default_registry(adapters=(), **kwargs)


# -- registry ---------------------------------------------------------------


def test_steps_run_in_dependency_order_within_registration_order():
    registry = StepRegistry(
        (
            StepSpec(id="c", title="c", run=_Counter("c"), depends_on=("a",)),
            StepSpec(id="b", title="b", run=_Counter("b")),
            StepSpec(id="a", title="a", run=_Counter("a")),
        )
    )
    assert [step.id for step in registry.ordered()] == ["b", "a", "c"]


def test_a_dependency_cycle_names_the_steps_it_involves():
    registry = StepRegistry(
        (
            StepSpec(id="a", title="a", run=_Counter("a"), depends_on=("b",)),
            StepSpec(id="b", title="b", run=_Counter("b"), depends_on=("a",)),
        )
    )
    with pytest.raises(InstallPlanError, match="cycle among: a, b"):
        registry.ordered()


def test_a_mistyped_dependency_fails_at_plan_time_with_the_id_that_was_written():
    registry = StepRegistry(
        (StepSpec(id="a", title="a", run=_Counter("a"), depends_on=("prereq.gti",)),)
    )
    with pytest.raises(InstallPlanError, match="a -> prereq.gti"):
        registry.ordered()


def test_registering_the_same_step_twice_is_refused():
    registry = StepRegistry((StepSpec(id="a", title="a", run=_Counter("a")),))
    with pytest.raises(InstallPlanError, match="duplicate step id: a"):
        registry.register(StepSpec(id="a", title="again", run=_Counter("a")))


def test_dependents_are_transitive_and_exclude_unrelated_steps():
    registry = StepRegistry(
        (
            StepSpec(id="a", title="a", run=_Counter("a")),
            StepSpec(id="b", title="b", run=_Counter("b"), depends_on=("a",)),
            StepSpec(id="c", title="c", run=_Counter("c"), depends_on=("b",)),
            StepSpec(id="unrelated", title="u", run=_Counter("unrelated")),
        )
    )
    assert registry.dependents_of("a") == ("a", "b", "c")
    assert registry.dependents_of("b", include_self=False) == ("c",)


def test_resource_merging_is_keyed_by_kind_and_id():
    first = ResourceRecord(kind="directory", id="/x", owned=True)
    again = ResourceRecord(kind="directory", id="/x", owned=True, reused=True)
    other = ResourceRecord(kind="command", id="/x", owned=False)
    merged = merge_resources((first,), (again, other))
    assert len(merged) == 2
    assert merged[0].reused is True


# -- capability gating and consent ------------------------------------------


def test_an_unselected_capability_is_skipped_with_its_reason_preserved(tmp_path):
    body = _Counter("optional")
    registry = StepRegistry(
        (StepSpec(id="optional", title="Optional", run=body, capability="dashboard"),)
    )
    result = _run(registry, tmp_path)
    assert body.calls == 0
    assert result.steps[0].state is StepState.SKIPPED
    assert "dashboard" in result.steps[0].summary
    assert result.outcome is InstallOutcome.READY


def test_selecting_the_capability_runs_the_step(tmp_path):
    body = _Counter("optional")
    registry = StepRegistry(
        (StepSpec(id="optional", title="Optional", run=body, capability="dashboard"),)
    )
    result = _run(registry, tmp_path, capabilities=frozenset({"dashboard"}))
    assert body.calls == 1
    assert result.steps[0].state is StepState.SUCCEEDED


def test_an_unattended_run_will_not_mutate_without_an_explicit_approval(tmp_path):
    body = _Counter("mutate")
    registry = StepRegistry(
        (StepSpec(id="mutate", title="Install a package", run=body, mutating=True),)
    )
    result = _run(registry, tmp_path, approve=frozenset())
    assert body.calls == 0
    assert result.outcome is InstallOutcome.NEEDS_USER
    assert result.exit_code == 10
    step = result.blocking_step
    assert step is not None and step.step_id == "mutate"
    assert "--approve mutate" in (step.remediation or "")


def test_naming_the_step_in_approve_authorises_only_that_step(tmp_path):
    allowed, refused = _Counter("allowed"), _Counter("refused")
    registry = StepRegistry(
        (
            StepSpec(id="allowed", title="Allowed", run=allowed, mutating=True),
            StepSpec(id="refused", title="Refused", run=refused, mutating=True),
        )
    )
    result = _run(registry, tmp_path, approve=frozenset({"allowed"}))
    assert (allowed.calls, refused.calls) == (1, 0)
    assert result.outcome is InstallOutcome.NEEDS_USER


def test_an_interactive_decline_skips_the_step_rather_than_failing_the_run(tmp_path):
    body = _Counter("mutate")
    registry = StepRegistry(
        (StepSpec(id="mutate", title="Install a package", run=body, mutating=True),)
    )
    engine = InstallEngine(
        registry,
        _options(tmp_path, interactive=True, approve=frozenset()),
        support=SUPPORTED,
        consent=lambda step: False,
        clock=_clock(),
    )
    result = engine.run()
    assert body.calls == 0
    assert result.steps[0].state is StepState.SKIPPED
    assert result.outcome is InstallOutcome.READY


# -- failure reporting ------------------------------------------------------


def test_a_failure_names_its_step_says_what_to_do_and_stops_the_run(tmp_path):
    later = _Counter("later")
    registry = StepRegistry(
        (
            StepSpec(
                id="first",
                title="First",
                run=lambda ctx: StepResult.failed(
                    "first", "postgresql refused the connection", "Start PostgreSQL, then rerun."
                ),
            ),
            StepSpec(id="later", title="Later", run=later, depends_on=("first",)),
        )
    )
    result = _run(registry, tmp_path)
    assert result.outcome is InstallOutcome.FAILED
    assert result.exit_code == 20
    assert result.blocking_step.step_id == "first"
    assert result.next_action == "Start PostgreSQL, then rerun."
    assert later.calls == 0
    assert result.steps[1].summary.startswith("not reached")


def test_a_step_that_raises_becomes_a_failure_naming_the_step_not_a_traceback(tmp_path):
    def explode(context):
        raise RuntimeError("apt-get is locked")

    registry = StepRegistry((StepSpec(id="boom", title="Boom", run=explode),))
    result = _run(registry, tmp_path)
    assert result.outcome is InstallOutcome.FAILED
    assert "RuntimeError: apt-get is locked" in result.steps[0].summary
    assert "--restart-from boom" in result.steps[0].remediation


def test_a_step_that_returns_the_wrong_shape_is_reported_against_its_owner(tmp_path):
    registry = StepRegistry(
        (StepSpec(id="bad", title="Bad", run=lambda ctx: "done", owner="platform.wsl"),)
    )
    result = _run(registry, tmp_path)
    assert result.outcome is InstallOutcome.FAILED
    assert "platform.wsl" in result.steps[0].remediation
    assert result.steps[0].retryable is False


def test_every_outcome_has_a_distinct_documented_exit_code():
    assert set(EXIT_CODES) == set(InstallOutcome)
    assert len(set(EXIT_CODES.values())) == len(EXIT_CODES)
    assert EXIT_CODES[InstallOutcome.READY] == 0


# -- rerun, resume and idempotence ------------------------------------------


def test_rerunning_after_a_failure_resumes_and_does_not_repeat_completed_work(tmp_path):
    first = _Counter("first")
    outcomes = iter(
        [
            StepResult.failed("second", "network unreachable", "Reconnect, then rerun."),
            StepResult.succeeded("second", "downloaded"),
        ]
    )
    second_calls = []

    def second(context):
        second_calls.append(context)
        return next(outcomes)

    registry = StepRegistry(
        (
            StepSpec(
                id="first",
                title="First",
                run=first,
                verify=lambda ctx: True,
            ),
            StepSpec(id="second", title="Second", run=second, depends_on=("first",)),
        )
    )

    failed = _run(registry, tmp_path)
    assert failed.outcome is InstallOutcome.FAILED

    recovered = _run(registry, tmp_path)
    assert recovered.outcome is InstallOutcome.READY
    assert first.calls == 1, "a verified, already-completed step must not run again"
    assert len(second_calls) == 2


def test_a_rerun_does_not_duplicate_the_resources_it_already_owns(tmp_path):
    directory = tmp_path / "data"
    registry = StepRegistry(
        (
            StepSpec(
                id="dir",
                title="Directory",
                mutating=True,
                run=lambda ctx: (
                    directory.mkdir(exist_ok=True),
                    StepResult.succeeded(
                        "dir",
                        "created",
                        resources=(ResourceRecord(kind="directory", id=str(directory)),),
                    ),
                )[1],
                verify=lambda ctx: directory.is_dir(),
            ),
        )
    )
    first = _run(registry, tmp_path)
    second = _run(registry, tmp_path)
    assert [record.id for record in first.resources] == [str(directory)]
    assert [record.id for record in second.resources] == [str(directory)]
    state = load_state(tmp_path / "install-state.json")
    assert len(state.resources) == 1


def test_a_completed_step_whose_condition_disappeared_is_executed_again(tmp_path):
    marker = tmp_path / "marker"
    body = _Counter("marker")

    def run(context):
        marker.write_text("x", encoding="utf-8")
        return body(context)

    registry = StepRegistry(
        (
            StepSpec(
                id="marker",
                title="Marker",
                run=run,
                verify=lambda ctx: marker.exists(),
            ),
        )
    )
    _run(registry, tmp_path)
    assert body.calls == 1
    _run(registry, tmp_path)
    assert body.calls == 1, "the observable condition still holds; revalidate, do not repeat"
    marker.unlink()
    _run(registry, tmp_path)
    assert body.calls == 2, "the condition is gone; the step must run again"


def test_a_verifier_that_raises_is_a_failure_with_a_restart_instruction(tmp_path):
    def boom(context):
        raise OSError("permission denied")

    registry = StepRegistry((StepSpec(id="v", title="V", run=_Counter("v"), verify=boom),))
    _run(registry, tmp_path)  # first run records success (verify is not called)
    result = _run(registry, tmp_path)
    assert result.outcome is InstallOutcome.FAILED
    assert "--restart-from v" in result.steps[0].remediation


def test_restart_from_redoes_the_step_and_its_dependents_only(tmp_path):
    counters = {name: _Counter(name) for name in ("a", "b", "c", "unrelated")}
    registry = StepRegistry(
        (
            StepSpec(id="a", title="a", run=counters["a"], verify=lambda ctx: True),
            StepSpec(
                id="b", title="b", run=counters["b"], depends_on=("a",), verify=lambda ctx: True
            ),
            StepSpec(
                id="c", title="c", run=counters["c"], depends_on=("b",), verify=lambda ctx: True
            ),
            StepSpec(id="unrelated", title="u", run=counters["unrelated"], verify=lambda ctx: True),
        )
    )
    _run(registry, tmp_path)
    result = _run(registry, tmp_path, restart_from="b")
    assert counters["a"].calls == 1
    assert counters["unrelated"].calls == 1
    assert counters["b"].calls == 2
    assert counters["c"].calls == 2
    assert any("restart-from" in message for message in result.messages)


def test_no_resume_ignores_the_record_without_deleting_it(tmp_path):
    body = _Counter("a")
    registry = StepRegistry((StepSpec(id="a", title="a", run=body, verify=lambda ctx: True),))
    _run(registry, tmp_path)
    _run(registry, tmp_path, resume=False)
    assert body.calls == 2
    assert (tmp_path / "install-state.json").exists()


# -- dry run ----------------------------------------------------------------


def test_a_dry_run_reports_the_plan_runs_read_only_checks_and_writes_nothing(tmp_path):
    check, mutate = _Counter("check"), _Counter("mutate")
    registry = StepRegistry(
        (
            StepSpec(id="check", title="Check", run=check),
            StepSpec(id="mutate", title="Mutate", run=mutate, mutating=True, depends_on=("check",)),
        )
    )
    result = _run(registry, tmp_path, dry_run=True)
    assert check.calls == 1
    assert mutate.calls == 0
    actions = {row.step_id: row.action for row in result.plan}
    assert actions == {"check": PlanAction.RUN, "mutate": PlanAction.WOULD_RUN}
    assert result.state_path is None
    assert not (tmp_path / "install-state.json").exists()


def test_a_dry_run_after_a_restart_shows_the_restart_without_changing_the_record(tmp_path):
    registry = StepRegistry(
        (StepSpec(id="a", title="a", run=_Counter("a"), verify=lambda ctx: True),)
    )
    _run(registry, tmp_path)
    before = (tmp_path / "install-state.json").read_text(encoding="utf-8")
    result = _run(registry, tmp_path, dry_run=True, restart_from="a")
    assert result.plan[0].action is PlanAction.RUN
    assert (tmp_path / "install-state.json").read_text(encoding="utf-8") == before


def test_each_executed_step_reports_how_long_it_took(tmp_path):
    ticks = iter([0.0, 0.25, 0.25, 0.5])
    registry = StepRegistry((StepSpec(id="a", title="a", run=_Counter("a")),))
    result = InstallEngine(
        registry,
        _options(tmp_path),
        support=SUPPORTED,
        clock=_clock(),
        timer=lambda: next(ticks),
    ).run()
    assert result.steps[0].duration_ms == 250


# -- host admission ---------------------------------------------------------


def test_an_unsupported_host_is_refused_before_any_step_runs(tmp_path):
    body = _Counter("mutate")
    registry = StepRegistry((StepSpec(id="mutate", title="Mutate", run=body, mutating=True),))
    engine = InstallEngine(registry, _options(tmp_path), support=UNSUPPORTED, clock=_clock())
    result = engine.run()
    assert result.outcome is InstallOutcome.UNSUPPORTED_HOST
    assert result.exit_code == 12
    assert body.calls == 0
    assert result.state_path is None
    assert not (tmp_path / "install-state.json").exists()
    assert result.next_action == UNSUPPORTED.remediation
    assert result.platform["installable"] is False


# -- invalid input ----------------------------------------------------------


def test_a_record_from_another_installer_version_asks_for_a_repair_plan(tmp_path):
    state_path = tmp_path / "install-state.json"
    save_state(
        InstallState(installer_version="0.9.0", target_version="0.9.0"),
        state_path,
        now="2026-01-01T00:00:00+00:00",
    )
    registry = StepRegistry((StepSpec(id="a", title="a", run=_Counter("a")),))
    result = _run(registry, tmp_path)
    assert result.outcome is InstallOutcome.INVALID_INPUT
    assert result.exit_code == 11
    assert "0.9.0" in result.next_action
    assert state_path.exists(), "an incompatible record is reported, never deleted"


def test_an_unreadable_record_is_reported_rather_than_silently_restarted(tmp_path):
    state_path = tmp_path / "install-state.json"
    state_path.write_text("{not json", encoding="utf-8")
    registry = StepRegistry((StepSpec(id="a", title="a", run=_Counter("a")),))
    result = _run(registry, tmp_path)
    assert result.outcome is InstallOutcome.INVALID_INPUT
    assert "--fresh" in result.next_action
    assert state_path.read_text(encoding="utf-8") == "{not json"


def test_fresh_starts_a_new_record_over_an_incompatible_one(tmp_path):
    state_path = tmp_path / "install-state.json"
    save_state(
        InstallState(installer_version="0.9.0", target_version="0.9.0"),
        state_path,
        now="2026-01-01T00:00:00+00:00",
    )
    registry = StepRegistry((StepSpec(id="a", title="a", run=_Counter("a")),))
    result = _run(registry, tmp_path, fresh=True)
    assert result.outcome is InstallOutcome.READY
    assert load_state(state_path).installer_version == "1.2.3"


def test_an_unknown_restart_target_is_invalid_input_not_a_crash(tmp_path):
    registry = StepRegistry((StepSpec(id="a", title="a", run=_Counter("a")),))
    result = _run(registry, tmp_path, restart_from="nope")
    assert result.outcome is InstallOutcome.INVALID_INPUT
    assert "nope" in result.next_action


def test_a_record_with_an_unknown_schema_is_not_read(tmp_path):
    state_path = tmp_path / "install-state.json"
    state_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(IncompatibleStateError):
        load_state(state_path)


def test_a_record_that_is_not_an_object_is_a_state_error(tmp_path):
    state_path = tmp_path / "install-state.json"
    state_path.write_text("[]", encoding="utf-8")
    with pytest.raises(StateError):
        load_state(state_path)


# -- progress ---------------------------------------------------------------


def test_progress_reports_a_start_and_a_terminal_state_for_each_step(tmp_path):
    events: list[ProgressEvent] = []
    registry = StepRegistry(
        (
            StepSpec(id="a", title="A", run=_Counter("a")),
            StepSpec(id="b", title="B", run=_Counter("b")),
        )
    )
    InstallEngine(
        registry,
        _options(tmp_path),
        support=SUPPORTED,
        progress=events.append,
        clock=_clock(),
    ).run()
    assert [(event.step_id, event.phase) for event in events] == [
        ("a", "start"),
        ("a", "done"),
        ("b", "start"),
        ("b", "done"),
    ]
    assert events[1].state is StepState.SUCCEEDED
    assert events[-1].total == 2


# -- the record carries no secrets ------------------------------------------


def test_secret_shaped_keys_and_values_are_redacted():
    payload = {
        "dsn": "postgresql://aq:hunter2@localhost/aq",
        "api_key": "sk-abcdefghijklmnop",
        "note": "sk-abcdefghijklmnop",
        "authenticated": True,
        "auth_method": "browser",
        "path": "/home/user/.agent-queue",
    }
    redacted = redact(payload)
    assert redacted["dsn"] == "[redacted]"
    assert redacted["api_key"] == "[redacted]"
    assert redacted["note"] == "[redacted]", "a token is a token whatever the key is called"
    assert redacted["authenticated"] is True
    assert redacted["auth_method"] == "browser"
    assert redacted["path"] == "/home/user/.agent-queue"
    assert find_secrets(redacted) == []


def test_a_step_that_leaks_a_credential_fails_the_write_instead_of_persisting_it(tmp_path):
    state = InstallState(installer_version="1.2.3", target_version="1.2.3")
    state.resources[("database", "aq")] = ResourceRecord(
        kind="database",
        id="aq",
        detail={"password": "hunter2"},
    )
    path = tmp_path / "install-state.json"
    with pytest.raises(SecretLeakError) as error:
        # ``save_state`` redacts first, so reaching the fence means proving the
        # fence itself: assert directly on the unredacted payload.
        assert_secret_free(state.to_dict(), context="test")
    assert "password" in str(error.value)
    save_state(state, path, now="2026-01-01T00:00:00+00:00")
    assert "hunter2" not in path.read_text(encoding="utf-8")


def test_the_record_written_by_a_real_run_contains_no_secrets(tmp_path):
    registry = prerequisites_only(
        which=lambda name: f"/usr/bin/{name}", state_dir=tmp_path / "data"
    )
    result = _run(registry, tmp_path)
    assert result.outcome is InstallOutcome.READY
    payload = json.loads((tmp_path / "install-state.json").read_text(encoding="utf-8"))
    assert find_secrets(payload) == []


def test_the_record_is_written_atomically_and_owner_only(tmp_path):
    path = tmp_path / "install-state.json"
    save_state(
        InstallState(installer_version="1.2.3", target_version="1.2.3"),
        path,
        now="2026-01-01T00:00:00+00:00",
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".install-state-*")), "no temporary file is left behind"


# -- built-in prerequisites -------------------------------------------------


def test_the_built_in_registry_admits_the_host_before_it_checks_anything_else():
    registry = default_registry()
    assert registry.ordered()[0].id == STEP_HOST
    # Adapters chain onto each other rather than all naming the host step — a
    # provider login waits on its CLI, a PostgreSQL step on the one before it —
    # so the invariant is that every step depends on admission *transitively*,
    # which is exactly what ``dependents_of`` computes.
    assert set(registry.dependents_of(STEP_HOST)) == set(registry.ids())
    assert registry.get(STEP_DATA_DIR).mutating is True
    assert registry.get(STEP_PYTHON).mutating is False


def test_a_missing_prerequisite_command_gives_platform_specific_instructions(tmp_path):
    registry = prerequisites_only(which=lambda name: None, state_dir=tmp_path / "data")
    result = _run(registry, tmp_path)
    assert result.outcome is InstallOutcome.FAILED
    failure = result.blocking_step
    assert failure.step_id == STEP_GIT
    assert "apt-get install -y git" in failure.remediation, "WSL2 host gets the WSL2 command"


def test_the_data_directory_step_reuses_an_existing_directory_rather_than_owning_it(tmp_path):
    existing = tmp_path / "data"
    existing.mkdir()
    registry = prerequisites_only(which=lambda name: f"/usr/bin/{name}", state_dir=existing)
    result = _run(registry, tmp_path)
    record = next(item for item in result.resources if item.kind == "directory")
    assert (record.owned, record.reused) == (False, True)


def test_the_data_directory_step_owns_a_directory_it_created(tmp_path):
    target = tmp_path / "data"
    registry = prerequisites_only(which=lambda name: f"/usr/bin/{name}", state_dir=target)
    result = _run(registry, tmp_path)
    record = next(item for item in result.resources if item.kind == "directory")
    assert (record.owned, record.reused) == (True, False)
    assert target.is_dir()


def test_an_unwritable_data_directory_names_the_path_and_the_fix(tmp_path):
    target = tmp_path / "data"
    target.mkdir(mode=0o500)
    try:
        registry = prerequisites_only(which=lambda name: f"/usr/bin/{name}", state_dir=target)
        result = _run(registry, tmp_path)
    finally:
        target.chmod(0o700)
    if result.outcome is InstallOutcome.READY:  # running as root: the check cannot fail
        pytest.skip("this user can write to a mode-0500 directory")
    assert result.blocking_step.step_id == STEP_DATA_DIR
    assert str(target) in result.blocking_step.remediation
