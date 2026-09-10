"""The short onboarding wizard: a few questions in, one summary out.

``aq install`` is one command, not two experiences.  The engine already knows
how to run every step; what a newcomer additionally needs is (a) to be asked
only the handful of things that cannot be inferred, and (b) to be told, at the
end, what is ready, where their data lives and which URL to open.

Both halves live here as pure functions over plain values:

* :func:`question_plan` turns what was *observed* about the machine — which
  provider CLIs exist, whether a PostgreSQL server answers — into a short list
  of questions with good defaults.  Pressing Enter through it is the supported
  path; ``--advanced`` adds the optional extras rather than lengthening the
  default flow.
* :func:`summarize` turns the engine's :class:`~src.install.results.InstallResult`
  into the closing summary.  It reads only the result and the step details the
  onboarding steps recorded, so the human text and the ``--json`` payload are
  the same facts.

Nothing here prompts, prints, or touches a file: the CLI binds the questions to
a terminal and the summary to a console, and a test binds them to values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .logins import AuthProbe
from .onboarding import (
    CAPABILITY_DAEMON,
    CAPABILITY_DISCORD,
    STEP_CHECK,
    STEP_DAEMON,
    STEP_DASHBOARD,
    DashboardInfo,
    Location,
)
from .postgres_steps import CAPABILITY_MANAGED, CAPABILITY_ROTATE, STEP_CONNECTION
from .prerequisites import STEP_GIT, STEP_TMUX
from .providers import ProviderInstaller, provider_installers
from .results import InstallOutcome, InstallResult, StepState


@dataclass(frozen=True, slots=True)
class Question:
    """One yes/no choice, its default, and the capability it selects."""

    id: str
    prompt: str
    capability: str
    default: bool
    #: Why this default — shown under the prompt so the answer is informed
    #: rather than guessed.
    detail: str = ""
    #: Advanced questions are asked only under ``--advanced``; their defaults
    #: are what an ordinary run uses.
    advanced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "capability": self.capability,
            "default": self.default,
            "detail": self.detail,
            "advanced": self.advanced,
        }


def question_plan(
    *,
    probes: Sequence[AuthProbe] = (),
    installers: Iterable[ProviderInstaller] | None = None,
    postgres_reachable: bool | None = None,
    advanced: bool = False,
) -> tuple[Question, ...]:
    """The questions to ask on this machine, in the order to ask them.

    Defaults are observations, not opinions: a harness that is already
    installed is offered pre-selected, a machine with no reachable PostgreSQL
    is offered a managed one, and Discord — which AQ does not need — defaults
    to no and is only asked at all under ``--advanced``.
    """
    by_provider = {probe.provider_id: probe for probe in probes}
    catalog = tuple(installers or provider_installers())
    anything_installed = any(
        probe.installed for probe in by_provider.values() if probe is not None
    )

    questions: list[Question] = []
    for installer in catalog:
        probe = by_provider.get(installer.id)
        if probe is not None and probe.installed:
            detail = f"already installed{'' if probe.authenticated else '; not signed in yet'}"
            default = True
        elif anything_installed:
            detail = "not installed here; AQ would install it with the provider's own installer"
            default = False
        else:
            # Nothing is installed, so the machine needs at least one harness
            # to be able to run a task at all.  Offer the first one.
            detail = "no coding-agent CLI is installed yet; AQ needs at least one to run a task"
            default = installer is catalog[0]
        questions.append(
            Question(
                id=f"provider.{installer.id}",
                prompt=f"Use {installer.title}?",
                capability=installer.capability,
                default=default,
                detail=detail,
            )
        )

    questions.append(
        Question(
            id="postgres-managed",
            prompt="Let AQ install and run a local PostgreSQL server?",
            capability=CAPABILITY_MANAGED,
            default=postgres_reachable is False,
            detail=(
                "no server answered on the configured host and port"
                if postgres_reachable is False
                else "a server is already reachable; AQ will use it"
                if postgres_reachable
                else "AQ needs one database; answer no if you already run a server"
            ),
        )
    )
    questions.append(
        Question(
            id="daemon",
            prompt="Start the AQ daemon when setup finishes?",
            capability=CAPABILITY_DAEMON,
            default=True,
            detail="this is what serves the dashboard and runs your tasks",
        )
    )
    questions.append(
        Question(
            id="discord",
            prompt="Deliver digests and escalations to Discord?",
            capability=CAPABILITY_DISCORD,
            default=False,
            detail=(
                "optional and off by default — AQ is operated from the CLI and the dashboard, "
                "never from Discord. You can add it later with `aq install --with discord`."
            ),
            advanced=True,
        )
    )
    return tuple(question for question in questions if advanced or not question.advanced)


def capabilities_for(
    questions: Iterable[Question], answers: Mapping[str, bool] | None = None
) -> frozenset[str]:
    """The capabilities selected by *answers*, falling back to each default.

    An unanswered question uses its default, which is what makes "press Enter
    through the wizard" and "take the recommended install" the same run.
    """
    answers = answers or {}
    return frozenset(
        question.capability
        for question in questions
        if bool(answers.get(question.id, question.default))
    )


# ---------------------------------------------------------------------------
# The closing summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OnboardingSummary:
    """What to tell the human once the run has stopped."""

    ready: bool
    headline: str
    locations: tuple[Location, ...] = ()
    dashboard: DashboardInfo | None = None
    #: Optional things this run did not do, each with how to add it later.
    skipped: tuple[str, ...] = ()
    #: What to do next, most urgent first.
    next_steps: tuple[str, ...] = ()
    #: A measured answer to whether this installation can run the first live task.
    readiness: FirstTaskReadiness | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "headline": self.headline,
            "locations": [location.to_dict() for location in self.locations],
            "dashboard": self.dashboard.to_dict() if self.dashboard else None,
            "skipped": list(self.skipped),
            "next_steps": list(self.next_steps),
            "readiness": self.readiness.to_dict() if self.readiness else None,
        }


@dataclass(frozen=True, slots=True)
class ReadinessCheck:
    """One observable prerequisite for a live first task.

    This is deliberately separate from an install step's state.  For example,
    an install may complete successfully with every provider declined, while a
    first task still cannot be claimed by a worker.
    """

    id: str
    label: str
    ready: bool
    detail: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "ready": self.ready,
            "status": "ready" if self.ready else "needs_attention",
            "detail": self.detail,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class FirstTaskReadiness:
    """The non-secret evidence needed before inviting an operator to spend on a task."""

    ready: bool
    checks: tuple[ReadinessCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"ready": self.ready, "checks": [check.to_dict() for check in self.checks]}


_CAPABILITY_LABELS: dict[str, str] = {
    CAPABILITY_DISCORD: "Discord delivery for digests and escalations",
    CAPABILITY_DAEMON: "starting the daemon",
    CAPABILITY_MANAGED: "installing a local PostgreSQL server",
}

#: Capabilities that exist for *repair*, not for setup.  Listing them among
#: "things you could also have installed" would invite a newcomer to rotate a
#: credential they just created.
_RECOVERY_CAPABILITIES: frozenset[str] = frozenset({CAPABILITY_ROTATE})


def _detail(result: InstallResult, step_id: str) -> dict[str, Any]:
    for step in result.steps:
        if step.step_id == step_id:
            return dict(step.detail)
    return {}


def _skipped_lines(result: InstallResult) -> tuple[str, ...]:
    """One line per optional thing this run did not do, and how to add it.

    A skipped capability is a *finished* install, not a partial one, so the
    summary says so explicitly rather than leaving a reader to wonder whether
    the missing harness or the missing Discord channel was a failure.
    """
    lines: list[str] = []
    for step in result.steps:
        if step.state is not StepState.SKIPPED:
            continue
        row = next((entry for entry in result.plan if entry.step_id == step.step_id), None)
        capability = row.capability if row else None
        if not capability or capability in result.capabilities:
            continue
        if capability in _RECOVERY_CAPABILITIES:
            continue
        label = _CAPABILITY_LABELS.get(capability)
        if label is None:
            if capability.startswith("provider."):
                label = f"the {capability.removeprefix('provider.')} harness"
            else:
                label = capability
        line = f"{label} — not selected; add it with `aq install --with {capability}`"
        if line not in lines:
            lines.append(line)
    return tuple(lines)


def _step_succeeded(result: InstallResult, step_id: str) -> bool:
    """Return whether this run observed one named prerequisite as satisfied."""
    return any(step.step_id == step_id and step.state is StepState.SUCCEEDED for step in result.steps)


#: Where an operator adds the first project root.  Named in one place because
#: the check's detail, its remediation and the closing next step all point at
#: the same page, and a newcomer should not have to reconcile three spellings.
PROJECT_ROOT_REMEDIATION = (
    "Add one under Settings → Project Roots in the dashboard, or put a `project_roots:` "
    "entry (`id`, `label`, `path`) in config.yaml with `aq system config edit`; then rerun "
    "`aq install` to recheck it."
)


def _project_root_check(result: InstallResult) -> ReadinessCheck:
    """Readiness condition 6's second half: a root project creation can use.

    AQ does not invent this path.  ``src/config_tuning.py`` derives defaults
    from cores and memory alone and deliberately owns no filesystem location,
    and the contract keeps project selection out of the installation wizard —
    so the honest installer behaviour is to *measure* the gap and name the page
    that closes it, not to write a directory into someone's home.
    """
    roots = _detail(result, STEP_CHECK).get("project_roots")
    if not isinstance(roots, list) or not roots:
        return ReadinessCheck(
            "project_root",
            "Project root",
            False,
            "No project root is configured, so `aq project onboard` has no `--root-id` to "
            "onboard into.",
            PROJECT_ROOT_REMEDIATION,
        )
    entries = [entry for entry in roots if isinstance(entry, Mapping)]
    usable = [
        entry for entry in entries if bool(entry.get("readable")) and bool(entry.get("writable"))
    ]
    if usable:
        named = ", ".join(str(entry.get("id") or "?") for entry in usable)
        return ReadinessCheck(
            "project_root",
            "Project root",
            True,
            f"{len(usable)} of {len(entries)} configured project root(s) are readable and "
            f"writable: {named}.",
            None,
        )
    named = ", ".join(
        f"{entry.get('id') or '?'} ({entry.get('path') or '?'})" for entry in entries
    )
    return ReadinessCheck(
        "project_root",
        "Project root",
        False,
        f"No configured project root is both readable and writable: {named}.",
        "Create the directory (or fix its permissions) on the daemon host, or correct the path "
        "under Settings → Project Roots, then rerun `aq install`.",
    )


def _first_task_readiness(
    result: InstallResult,
    *,
    probes: Sequence[AuthProbe],
    activations: Sequence[Any],
    dashboard: DashboardInfo | None,
) -> FirstTaskReadiness:
    """Turn install evidence into the explicit first-task admission checklist.

    The checks intentionally consume only non-secret probe results.  A profile
    being active proves both that its provider is authenticated and that AQ's
    catalog made it eligible for routing; neither requires examining a token
    or credential cache.
    """
    database = _step_succeeded(result, STEP_CONNECTION)
    daemon = _step_succeeded(result, STEP_DAEMON)
    dashboard_ready = bool(dashboard and dashboard.reachable)
    authenticated = any(probe.installed and probe.authenticated for probe in probes)
    routed = any(bool(getattr(activation, "active", False)) for activation in activations)
    workspace = all(
        _step_succeeded(result, step_id) for step_id in (STEP_CHECK, STEP_GIT, STEP_TMUX)
    )

    checks = (
        ReadinessCheck(
            "database", "Database", database,
            "AQ connected to its PostgreSQL database in this install run."
            if database else "AQ did not confirm a PostgreSQL connection in this install run.",
            None if database else "Run `aq doctor`, fix the reported database issue, then rerun `aq install`.",
        ),
        ReadinessCheck(
            "daemon", "Daemon", daemon,
            "The daemon answered its health endpoint." if daemon else "The daemon was not confirmed healthy.",
            None if daemon else "Run `aq start` or rerun `aq install` after fixing the reported failure.",
        ),
        ReadinessCheck(
            "dashboard", "Dashboard", dashboard_ready,
            f"The dashboard is reachable at {dashboard.url}." if dashboard_ready else
            "No reachable dashboard was observed (a source checkout may need its Vite server).",
            None if dashboard_ready else "Follow the dashboard hint above, then refresh the browser.",
        ),
        ReadinessCheck(
            "agent_authentication", "Agent authentication", authenticated,
            "At least one installed harness reported non-secret authentication evidence."
            if authenticated else "No installed harness reported authentication evidence.",
            None if authenticated else "Sign in to one selected harness, then rerun `aq install` to recheck it.",
        ),
        ReadinessCheck(
            "profile_routing", "Profile routing", routed,
            "At least one authenticated provider has an active worker profile."
            if routed else "No active worker profile can be routed to a live harness.",
            None if routed else "Rerun `aq install` after harness authentication, then check `aq agent list-profiles`.",
        ),
        ReadinessCheck(
            "workspace_prerequisites", "Workspace prerequisites", workspace,
            "Git, tmux, and AQ's configured worktree location were verified."
            if workspace else "Git, tmux, or AQ's configured worktree location was not verified.",
            None if workspace else "Fix the named prerequisite and rerun `aq install` before creating a project.",
        ),
        _project_root_check(result),
    )
    return FirstTaskReadiness(
        ready=result.outcome is InstallOutcome.READY and not result.dry_run and all(
            check.ready for check in checks
        ),
        checks=checks,
    )


def _next_steps(
    result: InstallResult,
    dashboard: DashboardInfo | None,
    readiness: FirstTaskReadiness,
) -> tuple[str, ...]:
    steps: list[str] = []
    if result.dry_run:
        # A dry run changed nothing, so the only honest next step is the run
        # that would.
        return (
            (
                "This was a dry run: nothing was changed. Rerun without `--dry-run` "
                "to carry out the plan above."
            ),
        )
    if result.outcome is InstallOutcome.NEEDS_USER:
        blocking = result.blocking_step
        subject = f"for {blocking.step_id}" if blocking else "named above"
        steps.append(
            f"Finish the action {subject} and rerun `aq install` — it resumes where it "
            "stopped and re-runs nothing that is already done."
        )
    elif result.outcome in (InstallOutcome.FAILED, InstallOutcome.UNSUPPORTED_HOST):
        steps.append("Fix the failure named above, then rerun `aq install`.")
    if result.outcome is InstallOutcome.READY:
        if dashboard and dashboard.reachable:
            steps.append(f"Open the dashboard at {dashboard.url}.")
        elif dashboard and dashboard.hint:
            steps.append(dashboard.hint)
        if readiness.ready:
            steps.append(
                "Create your first project and task: `aq project onboard --root-id <root> "
                "--help`, or follow docs/tutorials/first-task.md."
            )
        else:
            first_missing = next(check for check in readiness.checks if not check.ready)
            steps.append(
                "First-task readiness needs attention: "
                f"{first_missing.detail} {first_missing.remediation or ''}".rstrip()
            )
        steps.append("`aq doctor` checks this installation whenever something looks wrong.")
    return tuple(steps)


def summarize(
    result: InstallResult,
    *,
    probes: Sequence[AuthProbe] = (),
    activations: Sequence[Any] = (),
) -> OnboardingSummary:
    """Fold the engine's result into the closing summary.

    Everything comes from the result: the locations the ``config.check`` step
    recorded, the dashboard the ``daemon.dashboard`` step classified, and the
    outcome the engine classified.  There is no second source of truth to keep
    in step with the run.
    """
    check = _detail(result, STEP_CHECK)
    locations = tuple(
        Location(
            label=str(entry.get("label", "")),
            path=str(entry.get("path", "")),
            note=str(entry.get("note", "")),
        )
        for entry in check.get("locations") or ()
        if isinstance(entry, Mapping)
    )
    board = _detail(result, STEP_DASHBOARD).get("dashboard")
    dashboard = (
        DashboardInfo(
            url=str(board.get("url", "")),
            reachable=bool(board.get("reachable")),
            source=str(board.get("source", "unknown")),
            hint=str(board.get("hint", "")),
        )
        if isinstance(board, Mapping)
        else None
    )
    ready = result.outcome is InstallOutcome.READY and not result.dry_run
    if result.dry_run:
        headline = "Dry run: this is the plan, and nothing was changed."
    elif ready:
        headline = "AQ is installed and ready."
    else:
        headline = f"Installation stopped: {result.outcome.value}."
    readiness = _first_task_readiness(
        result, probes=probes, activations=activations, dashboard=dashboard
    )
    return OnboardingSummary(
        ready=ready,
        headline=headline,
        locations=locations,
        dashboard=dashboard,
        skipped=_skipped_lines(result),
        next_steps=_next_steps(result, dashboard, readiness),
        readiness=readiness,
    )


@dataclass(frozen=True, slots=True)
class WizardChoices:
    """The answers a front-end collected, and what they select."""

    questions: tuple[Question, ...] = ()
    answers: Mapping[str, bool] = field(default_factory=dict)

    @property
    def capabilities(self) -> frozenset[str]:
        return capabilities_for(self.questions, self.answers)


__all__ = [
    "PROJECT_ROOT_REMEDIATION",
    "FirstTaskReadiness",
    "OnboardingSummary",
    "Question",
    "ReadinessCheck",
    "WizardChoices",
    "capabilities_for",
    "question_plan",
    "summarize",
]
