"""Shadow-versus-legacy decision comparison for the guarded root cutover.

The reconciler's shadow mode (:mod:`src.integration.reconciler`) already
records, for every visit, the one decision the pinned policy table would have
made: ``rule``, ``primitive``, the observed fact binding and the exact
``policy_artifact_sha256`` it ran under. Legacy's own decisions are recorded by
the services that act, in the ledgers declared in :data:`LEGACY_LEDGER`. This
module joins the two arms over one explicit evidence window and renders the
result as the artifact an operator reads before any root subject is
transferred.

What this module deliberately does not do:

* It never re-derives legacy policy. The old arm is what legacy durably did,
  not a second implementation of how it decides.
* It never asserts that an observation period has elapsed. The window is
  ``recorded_at`` from the two arms; a window shorter than
  :data:`SHADOW_OBSERVATION_REQUIRED_SECONDS` is reported incomplete and blocks.
* It never mutates anything and never transfers a subject. Operator commands
  are rendered as text for a human to run.

Completeness is the point. Every legacy decision in the declared ledger and
window appears as exactly one row of the table, so the acceptance criterion --
no old decision missing from the table -- holds by construction rather than by
inspection. A legacy decision the new engine never chose is a *named* row:

``agree``
    the mapped root subject made a shadow decision of the same action class.
``divergent``
    the subject was observed but chose other classes; the operator reads it.
``missing``
    nothing could cover the decision. Blocking.
``no_route``
    nothing in the window chose that action class at all, so the pinned artifact
    has no route for it. Blocking: legacy acted where the new engine would not.
    Independent of the verdict, so a row may be both ``missing`` and
    ``no_route``.

``coverage_limits`` states what the comparison does not prove.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text

from src.database import tables as t
from src.integration.subjects import Primitive

#: The revision-2 observation period. A shorter window is reported incomplete;
#: nothing in this module can make it complete.
SHADOW_OBSERVATION_REQUIRED_SECONDS = 7 * 24 * 60 * 60

#: Coarse action classes, one per primitive. A legacy ledger row and a shadow
#: decision are compared at class level so that one primitive spelling cannot
#: manufacture a divergence an operator has to adjudicate by hand. Every
#: primitive is mapped; ``tests`` asserts the totality.
ACTION_CLASSES: Mapping[str, str] = {
    Primitive.OBSERVE_SUBJECT.value: "observe",
    Primitive.SEAL.value: "seal",
    Primitive.GIT_MATERIALIZE_REF.value: "ownership",
    Primitive.GIT_MERGE_MEMBERS.value: "build",
    Primitive.GIT_PRESERVE.value: "promote",
    Primitive.GIT_PUBLISH.value: "promote",
    Primitive.GIT_ANCESTRY.value: "observe",
    Primitive.CI_REQUEST.value: "ci",
    Primitive.CI_OBSERVE.value: "ci",
    Primitive.CI_ATTEST.value: "ci",
    Primitive.WRITER_FILE.value: "repair",
    Primitive.WRITER_LEASE.value: "repair",
    Primitive.WRITER_STOP_PROOF.value: "repair",
    Primitive.RECORD_RECEIPT.value: "repair",
    Primitive.RECORD_ATTEMPT.value: "repair",
    Primitive.RECORD_DECISION.value: "repair",
    Primitive.WAIT.value: "observe",
    Primitive.GATE.value: "human",
    Primitive.EJECT.value: "eject",
    Primitive.CLEANUP.value: "cleanup",
}

#: Classes with no legacy ledger counterpart. ``observe`` and ``wait`` are shadow
#: bookkeeping; ``human`` gates and ``eject`` have no per-batch legacy row. They
#: are reported, never blocked.
SHADOW_ONLY_CLASSES = frozenset({"observe", "human", "eject"})

#: Classes no primitive maps to. The shipped ``root-train`` decision table has no
#: route for publishing a candidate branch and its pull request, so every legacy
#: ``candidate_publish`` row in a window is a *finding*: the new engine would not
#: have published anything. A reviewed table that adds the route clears it.
LEGACY_ONLY_CLASSES = frozenset({"publish"})

#: Classes where an unknown fact changes a remote effect. Their unknown
#: observations are reported for review and are blocking until an operator
#: acknowledges the exact journal sequence; unknown is not a successful action.
UNKNOWN_SENSITIVE_CLASSES = frozenset(
    {"seal", "build", "publish", "ci", "promote", "cleanup", "ownership", "repair", "eject"}
)

VERDICT_AGREE = "agree"
VERDICT_DIVERGENT = "divergent"
VERDICT_MISSING = "missing"


def action_class(primitive: str | None) -> str:
    """The coarse class of a primitive name; ``unmapped`` when it is not one."""
    return ACTION_CLASSES.get(primitive or "", "unmapped")


@dataclass(frozen=True)
class LegacySource:
    """One declared legacy decision ledger and exactly how it is read.

    ``terminal`` selects the rows that record a decision legacy actually made
    rather than one it merely planned. ``time_column`` is the instant that
    decision became durable. ``batch_column`` is how the row reaches a root
    subject: the column holding its ``integration_batches`` id, ``"id"`` when
    the row *is* the batch, or ``None`` for the branch ledger, whose batch is
    the one owning the released ref.
    """

    key: str
    table: Any
    id_columns: tuple[str, ...]
    time_column: str
    terminal: str
    action_class: str
    batch_column: str | None
    description: str


LEGACY_LEDGER: tuple[LegacySource, ...] = (
    LegacySource(
        key="sealed_batch",
        table=t.integration_batches,
        id_columns=("id",),
        time_column="updated_at",
        terminal="base_sha IS NOT NULL AND lifecycle NOT IN ('empty', 'sealing')",
        action_class="seal",
        batch_column="id",
        description="A sealed legacy batch admitted its members onto the integration branch.",
    ),
    LegacySource(
        key="candidate_build",
        table=t.integration_candidate_revisions,
        id_columns=("batch_id", "revision"),
        time_column="updated_at",
        terminal="state IN ('built', 'testing', 'green', 'red', 'promoted')",
        action_class="build",
        batch_column="batch_id",
        description="Legacy merged the frozen members into a candidate revision.",
    ),
    LegacySource(
        key="candidate_publish",
        table=t.integration_candidate_publications,
        id_columns=("batch_id", "revision"),
        time_column="updated_at",
        terminal="state = 'pr_published'",
        action_class="publish",
        batch_column="batch_id",
        description="Legacy published the candidate branch and its pull request.",
    ),
    LegacySource(
        key="ci_attestation",
        table=t.integration_attestation_publications,
        id_columns=("id",),
        time_column="updated_at",
        terminal="state = 'published'",
        action_class="ci",
        batch_column="batch_id",
        description="Legacy published the CI attestation for a candidate revision.",
    ),
    LegacySource(
        key="repair_resolution",
        table=t.integration_candidate_resolutions,
        id_columns=("id",),
        time_column="updated_at",
        terminal="state IN ('accepted', 'rejected')",
        action_class="repair",
        batch_column="batch_id",
        description="Legacy accepted or rejected a repair resolution.",
    ),
    LegacySource(
        key="main_promotion",
        table=t.integration_promotion_intents,
        id_columns=("id",),
        time_column="committed_at",
        terminal="state = 'committed' AND root_batch_id IS NOT NULL",
        action_class="promote",
        batch_column="root_batch_id",
        description="Legacy committed a fenced promotion intent onto the default branch.",
    ),
    LegacySource(
        key="batch_release",
        table=t.integration_release_results,
        id_columns=("batch_id",),
        time_column="released_at",
        terminal="TRUE",
        action_class="promote",
        batch_column="batch_id",
        description="Legacy recorded the batch release result.",
    ),
    LegacySource(
        key="cleanup_item",
        table=t.integration_cleanup_items,
        id_columns=("batch_id", "kind", "identity"),
        time_column="terminal_at",
        terminal="state = 'complete' AND terminal_at IS NOT NULL",
        action_class="cleanup",
        batch_column="batch_id",
        description="Legacy completed a branch, pull request or worktree cleanup item.",
    ),
    LegacySource(
        key="ownership_release",
        table=t.integration_branch_owners,
        id_columns=("id",),
        time_column="updated_at",
        terminal="handoff_state = 'released'",
        action_class="ownership",
        batch_column=None,
        description="Legacy released the fenced ownership of a root's integration branch.",
    ),
)

#: What the comparison does not prove. Carried verbatim into every report.
COVERAGE_LIMITS: tuple[str, ...] = (
    (
        "Only the declared LEGACY_LEDGER sources are the old arm; a legacy decision "
        "recorded outside them is not compared and is never counted as agreement."
    ),
    (
        "Agreement is per action class over an evidence window, not per primitive and "
        "not a replay of legacy's internal rule selection."
    ),
    (
        "A shadow observation is a decision the pinned table would have made while "
        "legacy still held exclusive ownership; it proves nothing about the remote "
        "outcome of that action."
    ),
    (
        "Observation classes (observe, wait), human gates and ejections have no legacy "
        "ledger counterpart and are reported without blocking."
    ),
    (
        "The report is evidence for a human approval. It never transfers a subject, "
        "enables the active loop, or asserts an elapsed observation period it did not "
        "read from recorded timestamps."
    ),
)


@dataclass(frozen=True)
class LegacyDecision:
    """One durable legacy decision inside the evidence window."""

    source: str
    identity: str
    batch_id: str
    action_class: str
    recorded_at: float
    detail: str = ""
    subject_id: str | None = None


@dataclass(frozen=True)
class ShadowObservation:
    """One shadow decision journal entry inside the evidence window."""

    seq: int
    subject_id: str
    subject_version: int
    visit_id: str
    phase: str
    rule: str
    primitive: str
    action_class: str
    facts_digest: str
    policy_artifact_sha256: str
    recorded_at: float
    unknown: tuple[str, ...] = ()
    batch_id: str | None = None


@dataclass(frozen=True)
class ComparisonRow:
    """One table row: a legacy decision and the shadow decisions covering it.

    ``verdict`` is ``agree`` when the mapped root subject made a shadow decision
    of the same action class, ``divergent`` when it was observed but chose other
    classes, and ``missing`` when nothing could cover it. ``no_route`` is an
    independent, stronger finding: nothing in the window chose this class at
    all, so the pinned artifact has no route for it. A row can be both
    ``missing`` and ``no_route``.
    """

    legacy: LegacyDecision
    shadow: tuple[ShadowObservation, ...] = ()
    verdict: str = VERDICT_MISSING
    no_route: bool = False

    @property
    def missing(self) -> bool:
        return self.verdict == VERDICT_MISSING

    @property
    def blocking(self) -> bool:
        return self.verdict == VERDICT_MISSING or self.no_route

    def as_dict(self) -> dict[str, Any]:
        return {
            "old_decision": {
                "source": self.legacy.source,
                "identity": self.legacy.identity,
                "batch_id": self.legacy.batch_id,
                "action_class": self.legacy.action_class,
                "recorded_at": self.legacy.recorded_at,
                "detail": self.legacy.detail,
            },
            "new_decisions": [
                {
                    "seq": observation.seq,
                    "visit_id": observation.visit_id,
                    "subject_id": observation.subject_id,
                    "phase": observation.phase,
                    "rule": observation.rule,
                    "primitive": observation.primitive,
                    "facts_digest": observation.facts_digest,
                    "recorded_at": observation.recorded_at,
                }
                for observation in self.shadow
            ],
            "verdict": self.verdict,
            "no_route": self.no_route,
        }


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ObservationWindow:
    """The explicit window both arms are filtered by. Never inferred."""

    start: float
    end: float

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("an observation window must end after it starts")

    @property
    def elapsed_seconds(self) -> float:
        return self.end - self.start

    @property
    def complete(self) -> bool:
        return self.elapsed_seconds >= SHADOW_OBSERVATION_REQUIRED_SECONDS

    def contains(self, moment: float) -> bool:
        return self.start <= moment <= self.end

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "start_iso": _iso(self.start),
            "end_iso": _iso(self.end),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "required_seconds": SHADOW_OBSERVATION_REQUIRED_SECONDS,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class ShadowReport:
    """The checked comparison artifact for one evidence window.

    ``rows`` carries every legacy decision in the window exactly once, so the
    table cannot silently drop one. ``blocking_reasons`` is empty only when the
    window is complete, the pinned artifact is unambiguous, legacy held
    exclusive ownership throughout, every legacy decision is covered and every
    fact-sensitive unknown observation was acknowledged by the operator.
    """

    project_id: str
    window: ObservationWindow
    rows: tuple[ComparisonRow, ...]
    policy_artifacts: tuple[str, ...] = ()
    subject_ids: tuple[str, ...] = ()
    reconciler_owned_subjects: tuple[str, ...] = ()
    unexplained_batches: tuple[str, ...] = ()
    shadow_only: tuple[dict[str, Any], ...] = ()
    unacknowledged_unknowns: tuple[dict[str, Any], ...] = ()
    acknowledged_unknowns: tuple[dict[str, Any], ...] = ()
    first_observed_at: float | None = None
    last_observed_at: float | None = None

    @property
    def legacy_decisions(self) -> int:
        return len(self.rows)

    def _verdicts(self, verdict: str) -> int:
        return sum(1 for row in self.rows if row.verdict == verdict)

    @property
    def agreements(self) -> int:
        return self._verdicts(VERDICT_AGREE)

    @property
    def divergences(self) -> int:
        return self._verdicts(VERDICT_DIVERGENT)

    @property
    def missing_comparisons(self) -> int:
        return self._verdicts(VERDICT_MISSING)

    @property
    def unrouted_decisions(self) -> int:
        return sum(1 for row in self.rows if row.no_route)

    @property
    def unexplained(self) -> int:
        return len(self.unexplained_batches)

    @property
    def unknown_observations(self) -> int:
        return len(self.unacknowledged_unknowns)

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.policy_artifacts:
            reasons.append("the window contains no shadow decision to compare against")
        elif len(self.policy_artifacts) > 1:
            reasons.append(
                f"the window spans {len(self.policy_artifacts)} policy artifacts "
                f"({', '.join(self.policy_artifacts)}); run one window per artifact"
            )
        if not self.window.complete:
            reasons.append(
                f"the observation window covers {round(self.window.elapsed_seconds)}s of "
                f"the required {SHADOW_OBSERVATION_REQUIRED_SECONDS}s"
            )
        if self.reconciler_owned_subjects:
            reasons.append(
                f"the reconciler already owned {len(self.reconciler_owned_subjects)} root "
                "subject(s); a shadow week requires legacy to hold exclusive ownership"
            )
        if self.missing_comparisons:
            reasons.append(
                f"{self.missing_comparisons} legacy decision(s) have no shadow comparison"
            )
        if self.unrouted_decisions:
            reasons.append(
                f"{self.unrouted_decisions} legacy decision(s) have no route in the pinned "
                "policy artifact; the new engine would not have acted"
            )
        if self.unexplained:
            reasons.append(
                f"{self.unexplained} legacy batch(es) changed in the window with no "
                "accounted decision"
            )
        if self.unacknowledged_unknowns:
            reasons.append(
                f"{len(self.unacknowledged_unknowns)} unknown shadow observation(s) are "
                "unacknowledged; unknown is not a successful action"
            )
        return tuple(reasons)

    @property
    def cleared_for_review(self) -> bool:
        """Whether the evidence clears the tool's own gate.

        This never approves a cutover: a human still records the decision and
        the exact approval reference.
        """
        return not self.blocking_reasons

    @property
    def observed_span_seconds(self) -> float:
        if self.first_observed_at is None or self.last_observed_at is None:
            return 0.0
        return max(0.0, self.last_observed_at - self.first_observed_at)

    def canonical(self) -> str:
        """The canonical JSON the digest covers: the artifact's own identity."""
        return json.dumps(
            {
                "project_id": self.project_id,
                "window": self.window.as_dict(),
                "policy_artifacts": list(self.policy_artifacts),
                "rows": [row.as_dict() for row in self.rows],
                "unexplained_batches": list(self.unexplained_batches),
                "unacknowledged_unknowns": list(self.unacknowledged_unknowns),
                "shadow_only": list(self.shadow_only),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "report": "integration_shadow_comparison",
            "version": 1,
            "digest": self.digest,
            "project_id": self.project_id,
            "window": self.window.as_dict(),
            "observed": {
                "first_recorded_at": _iso(self.first_observed_at),
                "last_recorded_at": _iso(self.last_observed_at),
                "observed_span_seconds": round(self.observed_span_seconds, 3),
            },
            "counts": {
                "legacy_decisions": self.legacy_decisions,
                "agreements": self.agreements,
                "divergences": self.divergences,
                "missing_comparisons": self.missing_comparisons,
                "unrouted_decisions": self.unrouted_decisions,
                "unexplained_batches": self.unexplained,
                "unknown_observations": self.unknown_observations,
                "shadow_only_classes": len(self.shadow_only),
                "subjects": len(self.subject_ids),
            },
            "sources": [
                {
                    "key": source.key,
                    "table": source.table.name,
                    "time_column": source.time_column,
                    "terminal": source.terminal,
                    "action_class": source.action_class,
                    "description": source.description,
                }
                for source in LEGACY_LEDGER
            ],
            "canonicalisation": (
                "Both arms are grouped into coarse action classes before comparison and "
                "rows are ordered by recorded time, then source, then identity, so the "
                "same snapshot always renders the same table."
            ),
            "coverage_limits": list(COVERAGE_LIMITS),
            "policy_artifacts": list(self.policy_artifacts),
            "subjects": list(self.subject_ids),
            "reconciler_owned_subjects": list(self.reconciler_owned_subjects),
            "unexplained_batches": list(self.unexplained_batches),
            "unacknowledged_unknowns": list(self.unacknowledged_unknowns),
            "acknowledged_unknowns": list(self.acknowledged_unknowns),
            "shadow_only": list(self.shadow_only),
            "rows": [row.as_dict() for row in self.rows],
            "blocking_reasons": list(self.blocking_reasons),
            "cleared_for_review": self.cleared_for_review,
            "operator_commands": self.operator_commands(),
            "rollback_commands": self.rollback_commands(),
        }

    def operator_commands(self) -> list[str]:
        """The exact commands a human runs next; rendered, never executed."""
        evidence = self.digest
        commands = [
            "# 1. Re-read this artifact; a non-empty blocking_reasons means stop here.",
            f"aq integration shadow-report --project {self.project_id} --since-days 7",
            "# 2. Preview the exact root subjects and versions this evidence covers,",
            "#    then apply only after an explicit human approval is recorded.",
        ]
        commands.extend(
            f"aq integration engine-transfer REPOSITORY_ID --engine reconciler "
            f"--expected-subject {subject_id}:VERSION --reason 'approved shadow evidence "
            f"{evidence}' --evidence shadow-comparison:{evidence} "
            f"--evidence root-scenarios:SHA --apply"
            for subject_id in self.subject_ids
        )
        commands.extend(
            [
                "# 3. Only then set integration.reconciler_active: true and",
                "#    aq restart --no-dashboard.",
            ]
        )
        return commands

    def rollback_commands(self) -> list[str]:
        """The feature-off rollback, in the order a rollback must be applied."""
        subjects = self.reconciler_owned_subjects or self.subject_ids
        return [
            "# 1. Set integration.reconciler_active: false in ~/.agent-queue/config.yaml, then:",
            "aq restart --no-dashboard",
            "# 2. Return exclusive ownership to legacy for every transferred root subject:",
            *(
                f"aq integration engine-transfer REPOSITORY_ID --engine legacy "
                f"--expected-subject {subject_id}:VERSION --reason 'feature-off rollback' --apply"
                for subject_id in subjects
            ),
        ]

    def render_markdown(self) -> str:
        """The operator-facing artifact: the table plus the gates it failed."""
        window = self.window.as_dict()
        lines = [
            "# Shadow comparison report",
            "",
            f"Digest: `{self.digest}`",
            "",
            "## Evidence window",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| Project | {self.project_id} |",
            f"| Start (UTC) | {window['start_iso']} |",
            f"| End (UTC) | {window['end_iso']} |",
            (
                f"| Requested window | {round(window['elapsed_seconds'])}s of "
                f"{window['required_seconds']}s required |"
            ),
            f"| First recorded decision | {_iso(self.first_observed_at) or 'none'} |",
            f"| Last recorded decision | {_iso(self.last_observed_at) or 'none'} |",
            f"| Observed span | {round(self.observed_span_seconds)}s |",
            f"| Window complete | {'yes' if window['complete'] else 'NO'} |",
            f"| Pinned policy artifact(s) | {', '.join(self.policy_artifacts) or 'none'} |",
            f"| Root subjects compared | {len(self.subject_ids)} |",
            f"| Reconciler-owned subjects | {len(self.reconciler_owned_subjects)} |",
            "",
            "## Old and new decisions",
            "",
            (
                "Every legacy decision in the declared ledger and window appears exactly "
                "once below."
            ),
            "",
            "| Verdict | Meaning | Blocks |",
            "| --- | --- | --- |",
            "| `agree` | the subject made a shadow decision of the same class | no |",
            "| `divergent` | the subject was observed but chose other classes | no |",
            "| `missing` | nothing could cover the decision | yes |",
            "| `no_route` | nothing in the window chose that class at all | yes |",
            "",
            "| Old decision (legacy) | Class | Recorded (UTC) | New decision (shadow) | Verdict |",
            "| --- | --- | --- | --- | --- |",
        ]
        if not self.rows:
            lines.append(
                "| _no legacy decision in this window_ | | | | the window proves nothing |"
            )
        for row in self.rows:
            new = "<br>".join(
                f"`{observation.rule}` -> `{observation.primitive}` (seq {observation.seq})"
                for observation in row.shadow
            ) or "— none —"
            verdict = row.verdict + (" + no_route" if row.no_route else "")
            lines.append(
                f"| `{row.legacy.source}:{row.legacy.identity}` | {row.legacy.action_class} "
                f"| {_iso(row.legacy.recorded_at)} | {new} | {verdict} |"
            )
        lines.extend(
            [
                "",
                "## Counters",
                "",
                "| Counter | Value |",
                "| --- | --- |",
                f"| `legacy_decisions` | {self.legacy_decisions} |",
                f"| `agreements` | {self.agreements} |",
                f"| `divergences` | {self.divergences} |",
                f"| `missing_comparisons` | {self.missing_comparisons} |",
                f"| `unrouted_decisions` | {self.unrouted_decisions} |",
                f"| `unexplained` | {self.unexplained} |",
                f"| `unknown_observations` | {self.unknown_observations} |",
                "",
                "## Blocking rows",
                "",
            ]
        )
        blocking = [row for row in self.rows if row.blocking]
        lines.extend(
            f"- `{row.legacy.source}:{row.legacy.identity}` ({row.legacy.action_class}, batch "
            f"{row.legacy.batch_id}) — {row.verdict}"
            + (" + no_route" if row.no_route else "")
            + "."
            for row in blocking
        )
        if not blocking:
            lines.append("None.")
        lines.extend(["", "## Unexplained legacy activity", ""])
        lines.extend(
            f"- batch `{batch_id}` changed in the window with no accounted decision."
            for batch_id in self.unexplained_batches
        )
        if not self.unexplained_batches:
            lines.append("None.")
        lines.extend(["", "## Unknown shadow observations", ""])
        unknowns = (*self.unacknowledged_unknowns, *self.acknowledged_unknowns)
        lines.extend(
            f"- seq {item['seq']} (`{item['rule']}` -> `{item['primitive']}`): "
            f"{', '.join(item['unknown'])}"
            + (" — acknowledged" if item["acknowledged"] else " — UNACKNOWLEDGED")
            for item in unknowns
        )
        if not unknowns:
            lines.append("None.")
        lines.extend(["", "## Shadow-only classes", ""])
        lines.extend(
            f"- `{item['action_class']}` on {item['subject_id']}: {item['count']} "
            "observation(s); no legacy ledger counterpart, reported without blocking."
            for item in self.shadow_only
        )
        if not self.shadow_only:
            lines.append("None.")
        lines.extend(["", "## Coverage limits", ""])
        lines.extend(f"- {limit}" for limit in COVERAGE_LIMITS)
        lines.extend(["", "## Gates", ""])
        if self.blocking_reasons:
            lines.append("A non-empty list means the cutover is **not** approved:")
            lines.extend(f"- {reason}" for reason in self.blocking_reasons)
        else:
            lines.append(
                "This report clears its own gates. The cutover still needs a human decision."
            )
        lines.extend(["", "## Operator commands", "", "```sh", *self.operator_commands(), "```"])
        lines.extend(["", "## Rollback", "", "```sh", *self.rollback_commands(), "```"])
        lines.extend(
            [
                "",
                "## Signature",
                "",
                (
                    "A non-empty `blocking_reasons` list, an unacknowledged unknown "
                    "observation or a mixed artifact set means cutover is not approved. "
                    "Approval is a human decision recorded outside this report; the "
                    "report is evidence only."
                ),
                "",
                "- Approved for cutover by: ______________",
                "- Date (UTC): ______________",
                "- Commit SHA: ______________",
                f"- Report digest: {self.digest}",
                "",
            ]
        )
        return "\n".join(lines)


def compare(
    project_id: str,
    window: ObservationWindow,
    legacy: Sequence[LegacyDecision],
    observations: Sequence[ShadowObservation],
    *,
    touched_batches: Mapping[str, float] | None = None,
    engines: Mapping[str, str] | None = None,
    acknowledged_unknowns: Sequence[int] = (),
) -> ShadowReport:
    """Join both arms over ``window`` into the checked comparison artifact.

    Pure: the caller supplies both arms, so the whole comparison is testable
    without a database and the same snapshot always yields the same table.
    """
    acknowledged = set(acknowledged_unknowns)
    by_subject: dict[str, list[ShadowObservation]] = {}
    for observation in observations:
        if window.contains(observation.recorded_at):
            by_subject.setdefault(observation.subject_id, []).append(observation)
    batch_of = {
        observation.subject_id: observation.batch_id
        for subject_observations in by_subject.values()
        for observation in subject_observations
        if observation.batch_id
    }
    chosen_classes = {item.action_class for item in by_subject.values() for item in item}

    rows: list[ComparisonRow] = []
    accounted: set[str] = set()
    for decision in sorted(
        (item for item in legacy if window.contains(item.recorded_at)),
        key=lambda item: (item.recorded_at, item.source, item.identity),
    ):
        subject_id = decision.subject_id or _subject_for_batch(batch_of, decision)
        covering = tuple(
            observation
            for observation in by_subject.get(subject_id or "", ())
            if observation.action_class == decision.action_class
        )
        if covering:
            verdict = VERDICT_AGREE
        elif subject_id in by_subject:
            verdict = VERDICT_DIVERGENT
        else:
            verdict = VERDICT_MISSING
        accounted.add(decision.batch_id)
        rows.append(
            ComparisonRow(
                legacy=decision,
                shadow=covering,
                verdict=verdict,
                no_route=decision.action_class not in chosen_classes,
            )
        )

    unexplained = tuple(
        sorted(
            batch_id
            for batch_id, moment in (touched_batches or {}).items()
            if window.contains(moment) and batch_id not in accounted
        )
    )

    in_window = [item for item in observations if window.contains(item.recorded_at)]
    unacknowledged: list[dict[str, Any]] = []
    acknowledged_rows: list[dict[str, Any]] = []
    for observation in in_window:
        if not observation.unknown or observation.action_class not in UNKNOWN_SENSITIVE_CLASSES:
            continue
        item = {
            "seq": observation.seq,
            "subject_id": observation.subject_id,
            "rule": observation.rule,
            "primitive": observation.primitive,
            "unknown": list(observation.unknown),
            "acknowledged": observation.seq in acknowledged,
        }
        (acknowledged_rows if item["acknowledged"] else unacknowledged).append(item)

    covered_classes = {
        row.legacy.action_class for row in rows if not row.blocking
    }
    shadow_only = sorted(
        (
            {
                "subject_id": subject_id,
                "action_class": item.action_class,
                "count": sum(
                    1
                    for other in subject_observations
                    if other.action_class == item.action_class
                ),
            }
            for subject_id, subject_observations in by_subject.items()
            for item in subject_observations
            if item.action_class not in covered_classes
        ),
        key=lambda item: (item["subject_id"], item["action_class"]),
    )

    owners = {
        observation.subject_id
        for observation in in_window
        if (engines or {}).get(observation.subject_id, "legacy") != "legacy"
    }
    moments = [item.recorded_at for item in in_window] + [row.legacy.recorded_at for row in rows]
    return ShadowReport(
        project_id=project_id,
        window=window,
        rows=tuple(rows),
        policy_artifacts=tuple(sorted({item.policy_artifact_sha256 for item in in_window})),
        subject_ids=tuple(sorted(by_subject)),
        reconciler_owned_subjects=tuple(sorted(owners)),
        unexplained_batches=unexplained,
        shadow_only=tuple(shadow_only),
        unacknowledged_unknowns=tuple(unacknowledged),
        acknowledged_unknowns=tuple(acknowledged_rows),
        first_observed_at=min(moments) if moments else None,
        last_observed_at=max(moments) if moments else None,
    )


def _subject_for_batch(batch_of: Mapping[str, str | None], decision: LegacyDecision) -> str | None:
    for subject_id, batch_id in batch_of.items():
        if batch_id == decision.batch_id:
            return subject_id
    return None


# ------------------------------------------------------------------- read models


@dataclass(frozen=True)
class ShadowSnapshot:
    """One read-only PostgreSQL snapshot of both arms for a project."""

    legacy: tuple[LegacyDecision, ...] = ()
    observations: tuple[ShadowObservation, ...] = ()
    touched_batches: Mapping[str, float] = field(default_factory=dict)
    engines: Mapping[str, str] = field(default_factory=dict)


class ShadowReportReader:
    """A single read-only repeatable-read snapshot of both arms.

    Nothing here writes, locks or transfers; PostgreSQL enforces the read-only
    transaction. Legacy rows reach a root subject by their ``batch_id``,
    except the branch-ownership ledger, whose subject is the batch that owns
    the released ref.
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    async def read(self, project_id: str) -> ShadowSnapshot:
        async with self.db._engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                await conn.execute(text("SET TRANSACTION READ ONLY"))

                async def read(table, *conditions):
                    statement = select(table).where(*conditions)
                    rows = (await conn.execute(statement)).mappings().all()
                    return tuple(dict(row) for row in rows)

                subjects = await read(
                    t.integration_subjects,
                    t.integration_subjects.c.project_id == project_id,
                )
                engines = {row["id"]: row["engine"] for row in subjects}
                batch_ids = [row["batch_id"] for row in subjects if row["batch_id"]]
                if not batch_ids:
                    return ShadowSnapshot(engines=engines)
                batch_of = {row["id"]: row["batch_id"] for row in subjects if row["batch_id"]}
                subject_of = {batch_id: subject for subject, batch_id in batch_of.items()}
                batches = await read(
                    t.integration_batches, t.integration_batches.c.id.in_(batch_ids)
                )
                branches = {
                    (batch["repository_id"], batch["integration_branch"]): batch["id"]
                    for batch in batches
                    if batch["integration_branch"]
                }
                repository_ids = sorted({batch["repository_id"] for batch in batches})

                legacy: list[LegacyDecision] = []
                touched: dict[str, float] = {}
                for source in LEGACY_LEDGER:
                    for row in await self._source_rows(conn, source, batch_ids, repository_ids):
                        decision = self._decision(source, row, subject_of, branches)
                        if decision is None:
                            continue
                        legacy.append(decision)
                        touched[decision.batch_id] = max(
                            touched.get(decision.batch_id, 0.0), decision.recorded_at
                        )

                journal = await read(
                    t.integration_subject_journal,
                    t.integration_subject_journal.c.subject_id.in_(
                        [row["id"] for row in subjects]
                    ),
                    t.integration_subject_journal.c.mode == "shadow",
                    t.integration_subject_journal.c.entry_kind == "decision",
                )
        return ShadowSnapshot(
            legacy=tuple(legacy),
            observations=tuple(self._observation(row, batch_of) for row in journal),
            touched_batches=touched,
            engines=engines,
        )

    @staticmethod
    async def _source_rows(
        conn, source: LegacySource, batch_ids: list[str], repository_ids: list[str]
    ) -> tuple[dict[str, Any], ...]:
        conditions = []
        if source.batch_column == "id":
            conditions.append(source.table.c.id.in_(batch_ids))
        elif source.batch_column is not None:
            conditions.append(source.table.c[source.batch_column].in_(batch_ids))
        else:
            conditions.append(source.table.c.repository_id.in_(repository_ids))
        if source.terminal != "TRUE":
            conditions.append(text(source.terminal))
        rows = (await conn.execute(select(source.table).where(*conditions))).mappings().all()
        return tuple(dict(row) for row in rows)

    @staticmethod
    def _decision(
        source: LegacySource,
        row: Mapping[str, Any],
        subject_of: Mapping[str, str],
        branches: Mapping[tuple[str, str], str],
    ) -> LegacyDecision | None:
        moment = row.get(source.time_column)
        if moment is None:
            return None
        if source.batch_column is None:
            batch_id = branches.get((row["repository_id"], row["ref"]))
        elif source.batch_column == "id":
            batch_id = row["id"]
        else:
            batch_id = row[source.batch_column]
        if batch_id is None:
            return None
        return LegacyDecision(
            source=source.key,
            identity=":".join(str(row[name]) for name in source.id_columns),
            batch_id=batch_id,
            action_class=source.action_class,
            recorded_at=float(moment),
            detail=source.description,
            subject_id=subject_of.get(batch_id),
        )

    @staticmethod
    def _observation(
        row: Mapping[str, Any], batch_of: Mapping[str, str]
    ) -> ShadowObservation:
        payload = row.get("payload") or {}
        facts = payload.get("facts") or {}
        return ShadowObservation(
            seq=int(row["seq"]),
            subject_id=row["subject_id"],
            subject_version=int(row["subject_version"]),
            visit_id=row["visit_id"] or "",
            phase=row["phase"],
            rule=row["rule"] or "",
            primitive=row["primitive"] or "",
            action_class=action_class(row["primitive"]),
            facts_digest=row["facts_digest"] or "",
            policy_artifact_sha256=row["policy_artifact_sha256"],
            recorded_at=float(row["recorded_at"]),
            unknown=tuple(str(reason) for reason in (facts.get("unknown") or ())),
            batch_id=batch_of.get(row["subject_id"]),
        )


async def build_report(
    project_id: str,
    db: Any,
    *,
    since: float,
    until: float,
    acknowledged_unknowns: Sequence[int] = (),
) -> ShadowReport:
    """Read both arms once, then compare them over the explicit window."""
    snapshot = await ShadowReportReader(db).read(project_id)
    return compare(
        project_id,
        ObservationWindow(start=since, end=until),
        snapshot.legacy,
        snapshot.observations,
        touched_batches=snapshot.touched_batches,
        engines=snapshot.engines,
        acknowledged_unknowns=acknowledged_unknowns,
    )