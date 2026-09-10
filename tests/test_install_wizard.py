"""The short onboarding wizard: which questions get asked, and what gets said.

Both halves of :mod:`src.install.wizard` are pure, so this suite is about
judgement rather than plumbing: a default that misreads the machine, or a
summary that hides an optional thing the run skipped, is the failure a newcomer
would actually feel.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.install.logins import AuthProbe
from src.install.onboarding import CAPABILITY_DAEMON, CAPABILITY_DISCORD, STEP_CHECK, STEP_DASHBOARD
from src.install.postgres_steps import CAPABILITY_MANAGED
from src.install.results import (
    InstallOutcome,
    InstallResult,
    PlanAction,
    PlannedStep,
    StepResult,
    StepState,
)
from src.install.wizard import capabilities_for, question_plan, summarize


@pytest.fixture(autouse=True)
def _pg_backend():
    """Nothing here touches a database."""


def probe(provider_id: str, *, installed: bool, authenticated: bool = False) -> AuthProbe:
    return AuthProbe(
        provider_id=provider_id, installed=installed, authenticated=authenticated
    )


def question(questions, question_id):
    return next(item for item in questions if item.id == question_id)


# ---------------------------------------------------------------------------
# The questions
# ---------------------------------------------------------------------------


def test_the_short_set_is_short_and_never_asks_about_discord():
    questions = question_plan(probes=(), postgres_reachable=True)

    assert [item.id for item in questions] == [
        "provider.claude",
        "provider.codex",
        "provider.gemini",
        "postgres-managed",
        "daemon",
    ]
    assert all(item.advanced is False for item in questions)


def test_advanced_adds_discord_and_defaults_it_to_no():
    questions = question_plan(probes=(), postgres_reachable=True, advanced=True)

    discord = question(questions, "discord")
    assert discord.capability == CAPABILITY_DISCORD
    assert discord.default is False
    assert "optional" in discord.detail


def test_an_installed_harness_is_offered_preselected():
    questions = question_plan(
        probes=(
            probe("claude", installed=True, authenticated=True),
            probe("codex", installed=False),
            probe("gemini", installed=False),
        ),
        postgres_reachable=True,
    )

    assert question(questions, "provider.claude").default is True
    assert question(questions, "provider.codex").default is False
    assert "already installed" in question(questions, "provider.claude").detail


def test_a_harness_that_is_installed_but_not_signed_in_says_so():
    questions = question_plan(
        probes=(probe("claude", installed=True, authenticated=False),),
        postgres_reachable=True,
    )

    assert "not signed in yet" in question(questions, "provider.claude").detail


def test_a_machine_with_no_harness_is_offered_one_rather_than_none():
    """A fresh machine that selects nothing cannot run a task.

    The wizard is allowed to have an opinion here, and the opinion is the
    first supported harness — still a question, still answerable with "no".
    """
    questions = question_plan(probes=(), postgres_reachable=True)

    offered = [item.id for item in questions if item.id.startswith("provider.") and item.default]
    assert offered == ["provider.claude"]


def test_a_machine_with_no_database_is_offered_a_managed_one():
    reachable = question(question_plan(probes=(), postgres_reachable=True), "postgres-managed")
    missing = question(question_plan(probes=(), postgres_reachable=False), "postgres-managed")

    assert reachable.default is False
    assert missing.default is True
    assert missing.capability == CAPABILITY_MANAGED


def test_the_daemon_is_started_by_default():
    daemon = question(question_plan(probes=(), postgres_reachable=True), "daemon")

    assert daemon.default is True
    assert daemon.capability == CAPABILITY_DAEMON


# ---------------------------------------------------------------------------
# Answers to capabilities
# ---------------------------------------------------------------------------


def test_pressing_enter_through_the_wizard_takes_every_default():
    questions = question_plan(
        probes=(probe("claude", installed=True, authenticated=True),),
        postgres_reachable=False,
    )

    assert capabilities_for(questions, {}) == frozenset(
        {"provider.claude", CAPABILITY_MANAGED, CAPABILITY_DAEMON}
    )


def test_an_answer_overrides_the_default_in_both_directions():
    questions = question_plan(
        probes=(probe("claude", installed=True, authenticated=True),),
        postgres_reachable=True,
        advanced=True,
    )

    selected = capabilities_for(
        questions, {"provider.claude": False, "provider.gemini": True, "discord": True}
    )

    assert "provider.claude" not in selected
    assert "provider.gemini" in selected
    assert CAPABILITY_DISCORD in selected


# ---------------------------------------------------------------------------
# The closing summary
# ---------------------------------------------------------------------------


def result(
    *,
    outcome: InstallOutcome = InstallOutcome.READY,
    steps: tuple[StepResult, ...] = (),
    plan: tuple[PlannedStep, ...] = (),
    capabilities: tuple[str, ...] = (),
    dry_run: bool = False,
) -> InstallResult:
    return InstallResult(
        outcome=outcome,
        installer_version="1.2.3",
        target_version="1.2.3",
        dry_run=dry_run,
        interactive=True,
        platform={"host_path": "windows-wsl2"},
        capabilities=capabilities,
        plan=plan,
        steps=steps,
        resources=(),
        state_path="/home/you/.agent-queue/install-state.json",
    )


def check_step_result() -> StepResult:
    return StepResult.succeeded(
        STEP_CHECK,
        "config.yaml parses",
        detail={
            "locations": [
                {"label": "Configuration", "path": "/home/you/.agent-queue/config.yaml", "note": ""},
                {"label": "Vault", "path": "/home/you/.agent-queue/vault", "note": "markdown"},
            ]
        },
    )


def dashboard_step_result(*, reachable: bool = True) -> StepResult:
    return StepResult.succeeded(
        STEP_DASHBOARD,
        "open it",
        detail={
            "dashboard": {
                "url": "http://127.0.0.1:8081/dashboard" if reachable else "http://localhost:5173",
                "reachable": reachable,
                "source": "bundled" if reachable else "dev-server",
                "hint": "" if reachable else "Run `npm -w dashboard run dev`.",
            }
        },
    )


def first_task_machine_steps() -> tuple[StepResult, ...]:
    """Evidence that is independent from a provider's credential material."""
    return (
        check_step_result(),
        dashboard_step_result(),
        StepResult.succeeded("postgres.connection", "AQ connected to PostgreSQL"),
        StepResult.succeeded("daemon.start", "the daemon answered /health"),
        StepResult.succeeded("prereq.git", "git is available"),
        StepResult.succeeded("prereq.tmux", "tmux is available"),
    )


def test_a_ready_run_reports_where_data_lives_and_which_url_to_open():
    summary = summarize(
        result(steps=first_task_machine_steps()),
        probes=(probe("codex", installed=True, authenticated=True),),
        activations=(SimpleNamespace(active=True),),
    )

    assert summary.ready is True
    assert [location.label for location in summary.locations] == ["Configuration", "Vault"]
    assert summary.dashboard is not None
    assert summary.dashboard.url.endswith("/dashboard")
    assert any("Open the dashboard" in step for step in summary.next_steps)
    assert any("first-task" in step for step in summary.next_steps)


def test_a_source_checkout_gets_the_dev_server_instruction_as_a_next_step():
    summary = summarize(
        result(steps=(check_step_result(), dashboard_step_result(reachable=False)))
    )

    assert any("npm -w dashboard run dev" in step for step in summary.next_steps)


def test_readiness_requires_a_measured_database_daemon_dashboard_and_routable_agent():
    summary = summarize(
        result(steps=first_task_machine_steps()),
        probes=(probe("codex", installed=True, authenticated=True),),
        activations=(SimpleNamespace(active=True),),
    )

    assert summary.readiness is not None
    assert summary.readiness.ready is True
    assert [check.id for check in summary.readiness.checks] == [
        "database",
        "daemon",
        "dashboard",
        "agent_authentication",
        "profile_routing",
        "workspace_prerequisites",
    ]
    assert all(check.ready for check in summary.readiness.checks)
    assert any("Create your first project" in step for step in summary.next_steps)


def test_an_installed_daemon_does_not_claim_first_task_readiness_without_a_routed_profile():
    summary = summarize(
        result(steps=first_task_machine_steps()),
        probes=(probe("codex", installed=True, authenticated=False),),
        activations=(SimpleNamespace(active=False),),
    )

    assert summary.ready is True, "installation can complete with optional providers declined"
    assert summary.readiness is not None
    assert summary.readiness.ready is False
    routing = next(check for check in summary.readiness.checks if check.id == "profile_routing")
    assert routing.ready is False
    assert routing.remediation is not None
    assert not any("Create your first project" in step for step in summary.next_steps)
    assert any("First-task readiness needs attention" in step for step in summary.next_steps)


def test_an_unselected_optional_capability_is_reported_as_a_choice_not_a_gap():
    summary = summarize(
        result(
            steps=(
                StepResult.skipped("config.discord", "capability 'discord' was not selected"),
                check_step_result(),
                dashboard_step_result(),
            ),
            plan=(
                PlannedStep(
                    step_id="config.discord",
                    title="Deliver digests and escalations to Discord",
                    action=PlanAction.SKIP_NOT_SELECTED,
                    reason="capability 'discord' was not selected",
                    mutating=True,
                    capability=CAPABILITY_DISCORD,
                ),
            ),
        )
    )

    assert summary.ready is True
    assert summary.skipped == (
        (
            "Discord delivery for digests and escalations — not selected; "
            "add it with `aq install --with discord`"
        ),
    )


def test_a_skipped_provider_names_the_flag_that_would_add_it():
    summary = summarize(
        result(
            steps=(
                StepResult.skipped("provider.codex-cli", "not selected"),
                check_step_result(),
            ),
            plan=(
                PlannedStep(
                    step_id="provider.codex-cli",
                    title="Install Codex CLI",
                    action=PlanAction.SKIP_NOT_SELECTED,
                    reason="capability 'provider.codex' was not selected",
                    mutating=True,
                    capability="provider.codex",
                ),
            ),
        )
    )

    assert summary.skipped == (
        "the codex harness — not selected; add it with `aq install --with provider.codex`",
    )


def test_a_selected_capability_that_was_declined_is_not_reported_as_unselected():
    """A declined consent is a different story from an unselected capability.

    The summary's "not installed (optional)" list is about things the operator
    never asked for; a step they selected and then declined is reported by the
    engine's own step list, and duplicating it here would tell them to select
    something they already selected.
    """
    summary = summarize(
        result(
            capabilities=(CAPABILITY_DISCORD,),
            steps=(
                StepResult.skipped("config.discord", "declined by the operator"),
                check_step_result(),
            ),
            plan=(
                PlannedStep(
                    step_id="config.discord",
                    title="Deliver digests and escalations to Discord",
                    action=PlanAction.RUN,
                    reason="not yet satisfied",
                    mutating=True,
                    capability=CAPABILITY_DISCORD,
                ),
            ),
        )
    )

    assert summary.skipped == ()


def test_a_run_that_stopped_at_a_human_says_it_resumes_rather_than_restarts():
    summary = summarize(
        result(
            outcome=InstallOutcome.NEEDS_USER,
            steps=(
                StepResult.needs_user(
                    "provider.claude-login",
                    "Claude Code is installed but not authenticated",
                    "Run `claude auth login`, then rerun `aq install`.",
                ),
            ),
        )
    )

    assert summary.ready is False
    assert summary.next_steps
    first = summary.next_steps[0]
    assert "provider.claude-login" in first
    assert "resumes where it stopped" in first


def test_the_summary_is_json_serialisable_for_the_machine_readable_mode():
    import json

    summary = summarize(result(steps=(check_step_result(), dashboard_step_result())))
    payload = json.loads(json.dumps(summary.to_dict()))

    assert payload["ready"] is True
    assert payload["dashboard"]["source"] == "bundled"
    assert payload["locations"][0]["label"] == "Configuration"


def test_a_failed_run_is_never_reported_as_ready():
    summary = summarize(
        result(
            outcome=InstallOutcome.FAILED,
            steps=(StepResult.failed("config.check", "it does not parse", "fix it"),),
        )
    )

    assert summary.ready is False
    assert "failed" in summary.headline
    assert summary.next_steps == ("Fix the failure named above, then rerun `aq install`.",)


def test_a_run_with_no_onboarding_details_still_summarises():
    """The summary reads the result; a result without those steps is not a crash.

    ``--dry-run`` and an unsupported host both produce results with no
    onboarding step details at all.
    """
    summary = summarize(result(outcome=InstallOutcome.UNSUPPORTED_HOST))

    assert summary.locations == ()
    assert summary.dashboard is None
    assert summary.ready is False


def test_step_state_vocabulary_is_the_engines_not_the_wizards():
    """The wizard classifies nothing itself — a guard against a second source of truth."""
    import inspect

    from src.install import wizard

    source = inspect.getsource(wizard)
    assert "StepState.SKIPPED" in source
    assert not any(
        literal in source for literal in ('"succeeded"', '"needs_user"', '"failed"')
    )
    assert StepState.SKIPPED.value == "skipped"


def test_a_dry_run_is_never_called_ready_and_says_what_would_change_it():
    """`ready` means an installed machine; a dry run installed nothing."""
    summary = summarize(
        result(dry_run=True, steps=(check_step_result(), dashboard_step_result()))
    )

    assert summary.ready is False
    assert "Dry run" in summary.headline
    assert summary.next_steps == (
        (
            "This was a dry run: nothing was changed. Rerun without `--dry-run` "
            "to carry out the plan above."
        ),
    )
    # The locations are still worth reporting: they are where a real run would
    # put things, and they were read, not written.
    assert summary.locations


def test_a_recovery_capability_is_not_offered_as_something_to_install():
    """`postgres-rotate` replaces a password; it is not a thing a newcomer lacks."""
    summary = summarize(
        result(
            steps=(
                StepResult.skipped("postgres.rotate", "not selected"),
                check_step_result(),
            ),
            plan=(
                PlannedStep(
                    step_id="postgres.rotate",
                    title="Rotate the AQ role password",
                    action=PlanAction.SKIP_NOT_SELECTED,
                    reason="capability 'postgres-rotate' was not selected",
                    mutating=True,
                    capability="postgres-rotate",
                ),
            ),
        )
    )

    assert summary.skipped == ()
