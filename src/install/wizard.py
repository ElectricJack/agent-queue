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
    STEP_DASHBOARD,
    DashboardInfo,
    Location,
)
from .postgres_steps import CAPABILITY_MANAGED
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "headline": self.headline,
            "locations": [location.to_dict() for location in self.locations],
            "dashboard": self.dashboard.to_dict() if self.dashboard else None,
            "skipped": list(self.skipped),
            "next_steps": list(self.next_steps),
        }


_CAPABILITY_LABELS: dict[str, str] = {
    CAPABILITY_DISCORD: "Discord delivery for digests and escalations",
    CAPABILITY_DAEMON: "starting the daemon",
    CAPABILITY_MANAGED: "installing a local PostgreSQL server",
}


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


def _next_steps(result: InstallResult, dashboard: DashboardInfo | None) -> tuple[str, ...]:
    steps: list[str] = []
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
        steps.append(
            "Create your first project and task: `aq project onboard --help`, or follow "
            "docs/tutorials/first-task.md."
        )
        steps.append("`aq doctor` checks this installation whenever something looks wrong.")
    return tuple(steps)


def summarize(result: InstallResult) -> OnboardingSummary:
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
    ready = result.outcome is InstallOutcome.READY
    headline = (
        "AQ is installed and ready."
        if ready
        else f"Installation stopped: {result.outcome.value}."
    )
    return OnboardingSummary(
        ready=ready,
        headline=headline,
        locations=locations,
        dashboard=dashboard,
        skipped=_skipped_lines(result),
        next_steps=_next_steps(result, dashboard),
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
    "OnboardingSummary",
    "Question",
    "WizardChoices",
    "capabilities_for",
    "question_plan",
    "summarize",
]
