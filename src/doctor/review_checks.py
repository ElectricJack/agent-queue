"""Review consistency checks. Repairs replay immutable decision application.

Historical approved projections without feedback receipts remain held for explicit
reconciliation; doctor never manufactures approval from a state label. Diverged
vault files remain untouched. Pinned playbook effects use the same operation service.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.reviews.vault import (
    body_sha256,
    frontmatter_for,
    split_frontmatter,
)

logger = logging.getLogger(__name__)

CHECK_ID = "reviews.consistency"
PLAYBOOK_CHECK_ID = "reviews.playbook_artifacts"
OWNER = "reviews"

#: States whose gate is *meant* to stay open — a human is still to decide.
_OPENING_STATES = ("in_review", "changes_requested", "rejected")

_RESOLVED_BY = "doctor:reviews.consistency"


def _vault_root(ctx: DoctorContext) -> Path:
    root = getattr(ctx.config, "vault_root", None)
    if callable(root):
        root = root()
    if root is None:
        root = Path(ctx.config.data_dir) / "vault"
    return Path(root)


def _frontmatter_for(
    review: dict, playbook: dict | None = None, spec_kind: str | None = None,
) -> dict:
    """The frontmatter the service would write if it were to rewrite the file."""
    return frontmatter_for(review, playbook, spec_kind)


def _vault_file_state(root: Path, review: dict, current_sha256: str) -> str:
    """``"ok"`` / ``"missing"`` / ``"diverged"``; mirrors ``ReviewService.vault_state``."""
    file_path = root / review["vault_path"]
    try:
        text = file_path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return "missing"
    except (OSError, UnicodeDecodeError):
        if not file_path.exists() and not file_path.is_symlink():
            return "missing"
        return "diverged"
    _, body = split_frontmatter(text)
    return "ok" if body_sha256(body) == current_sha256 else "diverged"


async def _evaluate(ctx: DoctorContext, review: dict) -> dict:
    """One review's drift, keyed: gate/vault, both from ``ok``/``missing``.

    gate ∈ ok, none, missing, approved_gate_open, gate_resolved_unapproved,
         withdrawn_waiters
    vault ∈ ok, missing, diverged, none (no current revision to diff against)
    """
    out: dict = {"id": review["id"], "state": review["state"], "gate": "ok", "vault": "ok"}
    gate_id = review.get("gate_id")
    if not gate_id:
        out["gate"] = "none"
    else:
        gate = await ctx.db.get_gate(gate_id)
        if gate is None:
            out["gate"] = "missing"
        elif review["state"] == "approved" and gate["status"] == "open":
            out["gate"] = "approved_gate_open"
        elif review["state"] in _OPENING_STATES and gate["status"] == "resolved":
            out["gate"] = "gate_resolved_unapproved"
        elif review["state"] == "withdrawn" and gate["status"] == "open":
            waiters = sorted(await ctx.db.get_gate_waiters(gate_id))
            if waiters:
                out["gate"] = "withdrawn_waiters"
                out["waiters"] = waiters

    from sqlalchemy import select
    from src.database.tables import ask_decisions, escalation_actions

    async with ctx.db._engine.connect() as conn:
        unfinished = (await conn.execute(select(escalation_actions.c.id).join(
            ask_decisions, ask_decisions.c.id == escalation_actions.c.decision_id,
        ).where(
            ask_decisions.c.review_id == review["id"],
            ask_decisions.c.review_revision == review["current_revision"],
            escalation_actions.c.status != "succeeded",
        ))).scalars().all()
    out["pending_operations"] = list(unfinished)
    current = await ctx.db.get_review_revision(review["id"], review["current_revision"])
    if current is None:
        out["vault"] = "none"
    else:
        out["vault"] = _vault_file_state(_vault_root(ctx), review, current["content_sha256"])
    return out


async def _evaluate_all(ctx: DoctorContext) -> list[dict]:
    return [await _evaluate(ctx, r) for r in await ctx.db.list_reviews()]


async def _check(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(id=CHECK_ID, severity=Severity.INFO, detail="database not configured")

    evals = await _evaluate_all(ctx)
    data: dict = {}
    fixable = False
    for ev in evals:
        if ev["pending_operations"]:
            data.setdefault("pending_operations", {})[ev["id"]] = ev["pending_operations"]
            fixable = True
        if ev["gate"] in ("none", "missing"):
            data.setdefault("gate_missing", []).append(ev["id"])
        elif ev["gate"] == "approved_gate_open":
            data.setdefault("approved_gate_open", []).append(ev["id"])
            fixable = True
        elif ev["gate"] == "gate_resolved_unapproved":
            data.setdefault("gate_resolved_unapproved", []).append(ev["id"])
        elif ev["gate"] == "withdrawn_waiters":
            data.setdefault("withdrawn_waiters", {})[ev["id"]] = ev["waiters"]
        if ev["vault"] == "none":
            data.setdefault("vault_unknown", []).append(ev["id"])
        elif ev["vault"] == "missing":
            data.setdefault("vault_missing", []).append(ev["id"])
            fixable = True
        elif ev["vault"] == "diverged":
            data.setdefault("diverged", []).append(ev["id"])
    data = {k: v for k, v in data.items() if v}

    if data:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.WARN,
            detail=f"{len(data)} review state issue(s)",
            fixable=fixable,
            data=data,
        )
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.OK,
        detail=f"{len(evals)} review(s) in step with vault and gate",
        data=data,
    )


async def _fix(ctx: DoctorContext) -> CheckResult:
    from src.asks.operations import ReviewOperations

    handler = getattr(ctx, "handler", None)
    repaired, rewritten, held = [], [], {}
    if ctx.db is not None and handler is not None:
        operations = ReviewOperations(handler._review_service())
        for ev in await _evaluate_all(ctx):
            review = await ctx.db.get_review(ev["id"])
            if (ev["gate"] != "approved_gate_open" and ev["vault"] != "missing"
                    and not review.get("pending_decision_id") and not ev["pending_operations"]):
                continue
            result = await operations.reconcile_review(ev["id"])
            if result["application_state"] == "applied":
                repaired.append(ev["id"])
                if ev["vault"] == "missing":
                    rewritten.append(ev["id"])
            else:
                held[ev["id"]] = result.get("error", "Decision application is pending")
    result = await _check(ctx)
    result.fix_applied = bool(repaired)
    result.data.update(repaired=repaired, rewritten=rewritten, held=held)
    return result


async def _approved_playbook_pins(ctx: DoctorContext) -> list[tuple[dict, dict]]:
    """``(review, revision)`` for the latest approved review of each playbook."""
    latest: dict[str, tuple[dict, dict]] = {}
    for review in await ctx.db.list_reviews():
        if review["state"] != "approved" and not review.get("pending_decision_id"):
            continue
        revision = await ctx.db.get_review_revision(review["id"], review["current_revision"])
        pin = (revision or {}).get("playbook")
        if not pin or not revision.get("playbook_artifact"):
            continue
        key = f"{pin.get('scope')}:{pin.get('scope_identifier') or ''}:{pin['playbook_id']}"
        seen = latest.get(key)
        if seen is None or (review.get("decided_at") or 0) > (seen[0].get("decided_at") or 0):
            latest[key] = (review, revision)
    return [latest[key] for key in sorted(latest)]


async def _playbook_findings(ctx: DoctorContext) -> tuple[int, list[dict]]:
    pins = await _approved_playbook_pins(ctx)
    activations = await ctx.db.list_playbook_activations()
    findings: list[dict] = []
    for review, revision in pins:
        pin = revision["playbook"]
        sha = pin["artifact_sha256"]
        finding = {
            "review_id": review["id"],
            "revision": revision["revision"],
            "playbook_id": pin["playbook_id"],
            "artifact_sha256": sha,
        }
        if await ctx.db.get_playbook_artifact_row(sha) is None:
            findings.append({**finding, "problem": "not_stored"})
            continue
        mine = [row for row in activations if row["playbook_id"] == pin["playbook_id"]
                and row["scope"] == pin["scope"]
                and (row.get("scope_identifier") or "") == (pin.get("scope_identifier") or "")
                and row["enabled"]]
        if not any(row.get("active_artifact_sha256") == sha for row in mine):
            findings.append(
                {
                    **finding,
                    "problem": "not_activated",
                    "active_artifact_sha256": [row.get("active_artifact_sha256") for row in mine],
                    "next_step": (
                        f"aq playbook activate --playbook-id {pin['playbook_id']} "
                        f"--artifact-sha256 {sha}"
                    ),
                }
            )
    return len(pins), findings


async def _check_playbooks(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None or not hasattr(ctx.db, "list_playbook_activations"):
        return CheckResult(
            id=PLAYBOOK_CHECK_ID, severity=Severity.INFO, detail="database not configured"
        )
    checked, findings = await _playbook_findings(ctx)
    if not findings:
        return CheckResult(
            id=PLAYBOOK_CHECK_ID,
            severity=Severity.OK,
            detail=f"{checked} approved playbook review(s) stored and activated",
            data={"checked": checked},
        )
    not_stored = [f for f in findings if f["problem"] == "not_stored"]
    return CheckResult(
        id=PLAYBOOK_CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"{len(findings)} approved playbook review(s) are not live: "
            f"{len(not_stored)} with no stored artifact (--fix stores it), "
            f"{len(findings) - len(not_stored)} stored but not activated"
        ),
        fixable=bool(not_stored),
        data={"checked": checked, "findings": findings},
    )


async def _fix_playbooks(ctx: DoctorContext) -> CheckResult:
    from src.asks.operations import ReviewOperations

    handler = getattr(ctx, "handler", None)
    applied, held = [], {}
    if ctx.db is not None and handler is not None:
        operations = ReviewOperations(handler._review_service())
        for review, revision in await _approved_playbook_pins(ctx):
            result = await operations.reconcile_review(review["id"])
            if result["application_state"] == "applied":
                applied.append(review["id"])
            else:
                held[review["id"]] = result.get("error", "Decision application is pending")
    result = await _check_playbooks(ctx)
    result.fix_applied = bool(applied)
    result.data.update(reconciled=applied, held=held)
    return result


def review_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id=CHECK_ID,
            run=_check,
            fix=_fix,
            owner=OWNER,
        ),
        DoctorCheck(
            id=PLAYBOOK_CHECK_ID,
            run=_check_playbooks,
            fix=_fix_playbooks,
            owner=OWNER,
        ),
    ]


CHECKS = review_checks()
