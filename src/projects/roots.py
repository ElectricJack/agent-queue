"""One verdict on the configured project roots, for every surface that reports it.

Two operator surfaces answer the same question — *is there somewhere a project
can be created?* — from the same configuration: the ``projects.roots`` doctor
check (:mod:`src.doctor.project_checks`) and the installation wizard's
first-task readiness checklist (:mod:`src.install.wizard`).  They used to carry
separate copies of the rule and separate copies of the remediation text, and
drifted apart twice: an empty ``project_roots`` read as OK to ``aq doctor``
while the installer called it "needs attention", and a configured root that is
readable but not writable did the same.  A newcomer who ran both was told two
different things about one fact.

This module is the single home for that rule and its wording.  Callers hand it
:class:`RootFacts` — the doctor from live :class:`~src.config.ProjectRoot`
properties, the wizard from the booleans the config-check step recorded — and
map the returned state onto their own vocabulary (a
:class:`~src.doctor.models.Severity`, a readiness boolean).  Nothing here
touches the filesystem, so both surfaces classify exactly the same evidence.

Writability is the bar because that is what project creation needs:
``ProjectOnboardingService`` refuses a non-writable root for every source mode
but ``link`` (:mod:`src.projects.onboarding`).  A read-only root can still be
linked, which is why an unwritable root is a warning rather than an error.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

#: Where an operator adds the first project root.  Named in one place because
#: the doctor check's detail, the installer's readiness remediation and the
#: installer's closing next step all point at the same two destinations, and a
#: newcomer should not have to reconcile three spellings of them.
PROJECT_ROOT_REMEDIATION = (
    "Add one under Settings → Project Roots in the dashboard, or put a `project_roots:` "
    "entry (`id`, `label`, `path`) in config.yaml with `aq system config edit`."
)

#: How an operator repairs a root that *is* configured but cannot be used:
#: the path is gone, unreadable, or read-only for the daemon's user.
PROJECT_ROOT_ACCESS_REMEDIATION = (
    "Create the directory (or fix its permissions) on the daemon host, or correct the path "
    "under Settings → Project Roots."
)


class ProjectRootsState(Enum):
    """What the configured roots, taken together, admit right now.

    * ``OK`` — at least one root is readable and writable, so
      ``aq project onboard`` has a ``--root-id`` it can create into.
    * ``MISSING`` — nothing is configured.  Under-configured, not broken: the
      daemon runs fine, but onboarding has no destination to take.
    * ``UNREADABLE`` — a configured root vanished or lost read access after
      load.  The most serious state: configuration names a place that is no
      longer there.
    * ``UNUSABLE`` — every configured root is readable but none is writable,
      so only ``link``-mode onboarding could use them.
    """

    OK = "ok"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    UNUSABLE = "unusable"


@dataclass(frozen=True, slots=True)
class RootFacts:
    """One root as a surface observed it, with no filesystem access of its own."""

    id: str
    path: str
    readable: bool
    writable: bool

    @property
    def usable(self) -> bool:
        """True when a project can be *created* here, not merely linked."""
        return self.readable and self.writable


@dataclass(frozen=True, slots=True)
class ProjectRootsAssessment:
    """The shared verdict: one state, one detail sentence, one remediation."""

    state: ProjectRootsState
    detail: str
    remediation: str | None
    usable: tuple[RootFacts, ...] = ()
    offending: tuple[RootFacts, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state is ProjectRootsState.OK


def _named(roots: Iterable[RootFacts]) -> str:
    return ", ".join(f"{root.id or '?'} ({root.path or '?'})" for root in roots)


def assess_project_roots(roots: Iterable[RootFacts]) -> ProjectRootsAssessment:
    """Classify the configured roots once, for whichever surface is reporting.

    Order matters: an unreadable root is named before an unwritable one, since
    a path that disappeared is a different (and worse) problem than a path
    whose permissions are wrong, and reporting only the second would hide it.
    """
    entries = tuple(roots)
    if not entries:
        return ProjectRootsAssessment(
            state=ProjectRootsState.MISSING,
            detail=(
                "No project root is configured, so `aq project onboard` has no `--root-id` "
                "to onboard into."
            ),
            remediation=PROJECT_ROOT_REMEDIATION,
        )
    unreadable = tuple(root for root in entries if not root.readable)
    if unreadable:
        return ProjectRootsAssessment(
            state=ProjectRootsState.UNREADABLE,
            detail=(
                f"{len(unreadable)} of {len(entries)} configured project root(s) are missing "
                f"or unreadable: {_named(unreadable)}."
            ),
            remediation=PROJECT_ROOT_ACCESS_REMEDIATION,
            offending=unreadable,
        )
    usable = tuple(root for root in entries if root.usable)
    if not usable:
        return ProjectRootsAssessment(
            state=ProjectRootsState.UNUSABLE,
            detail=(
                "No configured project root is both readable and writable, so "
                "`aq project onboard` can only link an existing checkout: "
                f"{_named(entries)}."
            ),
            remediation=PROJECT_ROOT_ACCESS_REMEDIATION,
            offending=entries,
        )
    return ProjectRootsAssessment(
        state=ProjectRootsState.OK,
        detail=(
            f"{len(usable)} of {len(entries)} configured project root(s) are readable and "
            f"writable: {', '.join(root.id or '?' for root in usable)}."
        ),
        remediation=None,
        usable=usable,
    )
