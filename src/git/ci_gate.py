"""Read a GitHub PR's status-check rollup and decide whether it is green.

Why this exists
---------------
On 2026-09-03 PR #341 ("docs(client): sync README with pinned generator")
was merged with ``Tests (default)`` FAILURE in its rollup, landing exactly
the ``packages/aq-client/README.md`` regression that
``tests/test_api_client_contract.py`` exists to catch.  CI caught it; the
merge happened anyway.

Nothing was bypassed.  ``main`` carries no required status check at all —
the repository's only ruleset covers deletion and non-fast-forward — and
the fleet's own merge path (:meth:`GitCommandsMixin._cmd_pr_merge` →
:meth:`GitManager.amerge_pr`) shells ``gh pr merge`` without ever asking
what CI said.  A survey of PRs #324-#353 found 29 of the last 30 merges
red on ``Tests (default)``: red-CI merges were the norm, not an accident.

This module is the *pure* half of the fix — given the entries GitHub
reports for a PR, it says green / red / pending / unknown and names the
checks responsible.  :meth:`GitManager.apr_check_rollup` supplies the
entries and ``_cmd_pr_merge`` applies ``integration.merge_ci_policy`` to
the verdict.  Keeping the judgement here means the interesting cases
(duplicate check names, superseded runs, a required check that has not
reported yet) are testable without gh, a network, or a real PR.

Rollup shapes
-------------
``gh pr view --json statusCheckRollup`` mixes two entry types:

* ``CheckRun`` — GitHub Actions.  ``name`` plus ``status``
  (``QUEUED`` / ``IN_PROGRESS`` / ``COMPLETED``) and, once completed, a
  ``conclusion``.
* ``StatusContext`` — the older commit-status API.  ``context`` plus a
  flat ``state``.

Both are reduced to ``(name, state)`` pairs before anything is judged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Conclusions that mean "this check is satisfied".  ``NEUTRAL`` and
#: ``SKIPPED`` belong here: a job skipped by an ``if:`` guard, or one that
#: deliberately reports neutral, is not a failure and never blocks a merge
#: on GitHub's own required-checks implementation either.
PASS_STATES = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

#: Conclusions that mean "this check failed".  ``ACTION_REQUIRED`` and
#: ``STARTUP_FAILURE`` are failures the run never got past; ``ERROR`` is
#: the ``StatusContext`` spelling of ``FAILURE``.
FAIL_STATES = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"})

#: States that mean "no verdict yet".  ``EXPECTED`` is a ``StatusContext``
#: that a branch protection rule expects but nothing has posted.
PENDING_STATES = frozenset({"PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED", "EXPECTED"})

#: States that carry no information about the code.  A run cancelled by
#: the workflow's ``concurrency`` group (``.github/workflows/tests.yml``
#: cancels superseded runs on non-``main`` refs) says nothing about the
#: commit, so it is dropped whenever the *same check name* also has a
#: conclusive entry — which is why #341's ``FAILURE, CANCELLED`` pair reads
#: as red rather than as an ambiguous mix.
INCONCLUSIVE_STATES = frozenset({"CANCELLED", "STALE"})

#: Verdict values, worst first.  ``unknown`` is "the rollup could not be
#: read", which is distinct from "read, and nothing has reported yet".
GREEN = "green"
RED = "red"
PENDING = "pending"
UNKNOWN = "unknown"

#: Values ``integration.merge_ci_policy`` accepts.  ``off`` skips the probe
#: entirely, ``warn`` reports and merges anyway, ``required`` refuses a
#: merge that is not green — including one whose CI cannot be read.
MERGE_CI_POLICY_OFF = "off"
MERGE_CI_POLICY_WARN = "warn"
MERGE_CI_POLICY_REQUIRED = "required"
MERGE_CI_POLICIES = frozenset(
    {MERGE_CI_POLICY_OFF, MERGE_CI_POLICY_WARN, MERGE_CI_POLICY_REQUIRED}
)


@dataclass(frozen=True)
class CiVerdict:
    """What CI says about a PR head, and which checks said it.

    Attributes
    ----------
    state:
        One of :data:`GREEN`, :data:`RED`, :data:`PENDING`, :data:`UNKNOWN`.
    failing:
        Names of checks whose latest conclusive entry is a failure.
    pending:
        Names of checks that have started but not concluded (a name whose
        only entries are cancelled counts here — it never reached a
        verdict).
    missing:
        Names listed in ``required_checks`` that the rollup does not
        mention at all.  Treated as not-yet-reported rather than as a
        failure: the rollup cannot tell "will never run" from "has not
        started", and blocking is the safe reading of either.
    considered:
        Names actually judged, after any ``required_checks`` filter.
    """

    state: str
    failing: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    considered: tuple[str, ...] = field(default=())

    @property
    def is_green(self) -> bool:
        return self.state == GREEN

    def summary(self) -> str:
        """One line naming the checks behind the verdict, for logs and errors."""
        if self.state == UNKNOWN:
            return "CI status could not be read"
        if self.state == GREEN:
            if not self.considered:
                return "no checks to satisfy"
            return f"{len(self.considered)} check(s) passing"
        parts: list[str] = []
        if self.failing:
            parts.append("failing: " + ", ".join(self.failing))
        if self.pending:
            parts.append("still running: " + ", ".join(self.pending))
        if self.missing:
            parts.append("not reported: " + ", ".join(self.missing))
        return "; ".join(parts) or "no checks reported yet"


def normalize_entry(entry: dict) -> tuple[str, str] | None:
    """Reduce one rollup entry to ``(check_name, STATE)``.

    Returns ``None`` for an entry with no usable name — a malformed or
    future entry type must not be mistaken for a nameless failing check.
    """
    name = entry.get("name") or entry.get("context") or ""
    name = str(name).strip()
    if not name:
        return None
    status = str(entry.get("status") or "").upper()
    if status and status != "COMPLETED":
        # A CheckRun that has not completed has no conclusion yet; its
        # ``status`` is the only truth about it.
        return name, status
    state = entry.get("conclusion") or entry.get("state") or ""
    state = str(state).strip().upper()
    return name, state or "PENDING"


def _fold(states: list[str]) -> str:
    """Collapse one check name's entries into a single state.

    A check name appears more than once whenever the workflow runs on both
    ``push`` and ``pull_request``, or when a superseded run leaves a
    cancelled entry behind.  Cancelled/stale entries are dropped as long as
    something conclusive remains; after that, failure outranks pending
    outranks pass, so one red arm reddens the name.
    """
    conclusive = [s for s in states if s not in INCONCLUSIVE_STATES]
    if not conclusive:
        # Only cancelled runs: the check never reached a verdict.
        return "PENDING"
    if any(s in FAIL_STATES for s in conclusive):
        return "FAILURE"
    if any(s not in PASS_STATES for s in conclusive):
        # Pending, or a state this module has never seen.  Both mean
        # "not yet satisfied", never "satisfied".
        return "PENDING"
    return "SUCCESS"


def classify_rollup(
    entries: list[dict] | None,
    required_checks: list[str] | None = None,
) -> CiVerdict:
    """Judge a PR's status-check rollup.

    Args:
        entries: ``statusCheckRollup`` as ``gh pr view --json`` returns it,
            or ``None`` when it could not be fetched (gh missing, not
            authenticated, network down).
        required_checks: Only these check names are judged.  Empty or
            ``None`` means "every check the rollup reports", which is the
            strict reading and the right default for a repo whose whole
            matrix is meant to be green.

    Returns:
        A :class:`CiVerdict`.  ``UNKNOWN`` is returned only for
        ``entries is None`` — an empty list is a real answer ("nothing has
        reported"), and reads as :data:`PENDING`.
    """
    if not isinstance(entries, list):
        # ``None`` from :meth:`GitManager.apr_check_rollup`, or anything a
        # caller could not turn into entries.  Never a green.
        return CiVerdict(state=UNKNOWN)

    folded: dict[str, list[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pair = normalize_entry(entry)
        if pair is None:
            continue
        name, state = pair
        folded.setdefault(name, []).append(state)

    wanted = [c.strip() for c in (required_checks or []) if str(c).strip()]
    if wanted:
        missing = tuple(c for c in wanted if c not in folded)
        considered = {name: states for name, states in folded.items() if name in set(wanted)}
    else:
        missing = ()
        considered = folded

    failing: list[str] = []
    pending: list[str] = []
    for name in sorted(considered):
        state = _fold(considered[name])
        if state == "FAILURE":
            failing.append(name)
        elif state != "SUCCESS":
            pending.append(name)

    verdict_names = tuple(sorted(considered))
    if failing:
        state = RED
    elif pending or missing or not verdict_names:
        state = PENDING
    else:
        state = GREEN
    return CiVerdict(
        state=state,
        failing=tuple(failing),
        pending=tuple(pending),
        missing=missing,
        considered=verdict_names,
    )
