"""Consistency check for document reviews (``reviews.consistency``).

The review service keeps three things in step: the ``doc_reviews`` row, the
gate it opened (``await_id`` = the review id), and the vault file it writes
beside them.  The service guards against a *diverged* vault file, but three
ways of drifting past that guard remain and this check reconciles them:

* the vault file went missing (deleted, or a failed write the service logged
  and swallowed) — **fixed** by rewriting it from the current revision, and
  any *approved* reviews whose gate the fix should unblock get their gate
  resolved ``approved``;
* the review is ``approved`` but its gate is still ``open`` (``decide``
  swallows a failed ``resolve_gate`` and leaves it here, spec §10) — **fixed**
  by resolving the gate ``approved`` through the orchestrator.

Everything else is report-only (surfaced in ``data``, never touched):

* the gate was *resolved* while its review is not approved;
* the gate was deleted altogether;
* a task still waits on a *withdrawn* review's gate.

``--fix`` therefore only ever (a) resolves gates for approved reviews whose
gate is still open, and (b) rewrites vault files whose body is missing — both
idempotent.  Diverged files are left byte-identical on purpose.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.reviews.vault import body_sha256, render, split_frontmatter, write_atomic

logger = logging.getLogger(__name__)

CHECK_ID = "reviews.consistency"
OWNER = "reviews"

#: States whose gate is *meant* to stay open — a human is still to decide.
_OPENING_STATES = ("in_review", "changes_requested")

_RESOLVED_BY = "doctor:reviews.consistency"


def _vault_root(ctx: DoctorContext) -> Path:
    root = getattr(ctx.config, "vault_root", None)
    if callable(root):
        root = root()
    if root is None:
        root = Path(ctx.config.data_dir) / "vault"
    return Path(root)


def _frontmatter_for(review: dict) -> dict:
    """The frontmatter the service would write if it were to rewrite the file."""
    created = date.fromisoformat(time.strftime("%Y-%m-%d", time.localtime(review["created_at"])))
    return {
        "title": review["title"],
        "status": review["state"],
        "kind": review["kind"],
        "review": review["id"],
        "revision": review["current_revision"],
        "project": review["project_id"],
        "author_task": review["author_task_id"] or "",
        "date": created,
    }


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
    if ctx.db is None:
        return CheckResult(id=CHECK_ID, severity=Severity.INFO, detail="database not configured")

    root = _vault_root(ctx)
    by_id = {r["id"]: r for r in await ctx.db.list_reviews()}
    pre = {ev["id"]: ev for ev in await _evaluate_all(ctx)}

    resolved: list[str] = []
    for rid, ev in pre.items():
        if ev["gate"] != "approved_gate_open":
            continue
        review = by_id[rid]
        handler = getattr(ctx, "handler", None)
        orchestrator = getattr(handler, "orchestrator", None)
        if orchestrator is None:
            logger.warning("doctor: no orchestrator to resolve review %s gate", rid)
            continue
        try:
            await orchestrator._resolve_gate_and_emit(
                review["gate_id"], resolved_by=_RESOLVED_BY, resolution="approved"
            )
            resolved.append(rid)
        except Exception:
            logger.exception("doctor: resolving review %s gate failed", rid)

    rewritten: list[str] = []
    for rid, ev in pre.items():
        if ev["vault"] != "missing":
            continue
        review = by_id[rid]
        current = await ctx.db.get_review_revision(review["id"], review["current_revision"])
        if current is None:
            continue
        try:
            write_atomic(root / review["vault_path"], render(_frontmatter_for(review), current["content"]))
            rewritten.append(rid)
        except OSError:
            logger.exception("doctor: rewriting review %s vault file failed", rid)

    result = await _check(ctx)
    result.fix_applied = bool(resolved or rewritten)
    if resolved:
        result.data["resolved"] = resolved
    if rewritten:
        result.data["rewritten"] = rewritten
    return result


def review_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id=CHECK_ID,
            run=_check,
            fix=_fix,
            owner=OWNER,
        )
    ]


CHECKS = review_checks()
