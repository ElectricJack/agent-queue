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

``reviews.playbook_artifacts`` covers what approving a *playbook* review is
for.  Its revision pins a compiled Playbook V2 artifact, and approval stores
that artifact (and activates it when the review asked).  For the most recently
approved review of each playbook the check reports

* ``not_stored`` — no artifact row holds the pinned hash (storing failed at
  approval, or the pinned artifact no longer validates) — **fixed** by storing
  exactly the pinned bytes, through the same validation an import runs;
* ``not_activated`` — the artifact is stored, but no activation of the
  playbook points at it.  Report-only: activating changes running policy, so
  it stays a deliberate ``aq playbook activate``.

A review approved before revisions could pin an artifact names none, so it is
invisible here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.reviews.vault import (
    body_sha256,
    frontmatter_for,
    render,
    split_frontmatter,
    write_atomic,
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


def _frontmatter_for(review: dict, playbook: dict | None = None) -> dict:
    """The frontmatter the service would write if it were to rewrite the file."""
    return frontmatter_for(review, playbook)


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
            write_atomic(
                root / review["vault_path"],
                render(_frontmatter_for(review, current.get("playbook")), current["content"]),
            )
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


async def _approved_playbook_pins(ctx: DoctorContext) -> list[tuple[dict, dict]]:
    """``(review, revision)`` for the latest approved review of each playbook."""
    latest: dict[str, tuple[dict, dict]] = {}
    for review in await ctx.db.list_reviews(state="approved"):
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
        mine = [row for row in activations if row["playbook_id"] == pin["playbook_id"]]
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
    """Store each approved, unstored pinned artifact; never activate."""
    from src.reviews.service import PlaybookPin

    handler = getattr(ctx, "handler", None)
    store = getattr(handler, "_review_store_playbook", None)
    stored: list[str] = []
    failed: dict[str, str] = {}
    if ctx.db is not None and store is not None:
        for review, revision in await _approved_playbook_pins(ctx):
            pin = PlaybookPin.from_revision(revision)
            if await ctx.db.get_playbook_artifact_row(pin.meta["artifact_sha256"]) is not None:
                continue
            result = await store(review, revision["revision"], pin)
            if result.get("success"):
                stored.append(review["id"])
            else:
                failed[review["id"]] = str(result.get("error"))
    result = await _check_playbooks(ctx)
    result.fix_applied = bool(stored)
    if stored:
        result.data["stored"] = stored
    if failed:
        result.data["store_failed"] = failed
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
