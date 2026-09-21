"""Tests for the reviews.consistency doctor check (Task 9).

Covers spec §10 of
``vault/projects/agent-queue/specs/2026-09-21-document-review-design.md``:
review state, gate state, and the vault file drift apart for three specific
reasons —
* a vault file that went missing,
* an approved review whose gate the service could not resolve (``decide``
  swallows a failed ``resolve_gate``),

* a gate that was resolved or withdrawn while the review stayed in an open
  state —

while a *diverged* body is left byte-identical on purpose (spec §7).  The
check must classify each case into the right severity and the right ``data``
bucket, and ``--fix`` must be idempotent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig
from src.database import Database
from src.doctor import default_registry
from src.doctor.models import DoctorContext, Severity
from src.doctor.review_checks import CHECK_ID, _check, _fix, _frontmatter_for
from src.event_bus import EventBus
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator
from src.reviews.vault import body_sha256, render, split_frontmatter
from tests.db_fixtures import lease_dsn

PROJECT = "p"
DOC = "# Doc\n\nIntro.\n"
NOW = 1_800_000_000.0


@pytest.fixture
def dsn():
    return lease_dsn("review_doctor.db")


@pytest.fixture
async def db(dsn):
    database = Database(dsn)
    await database.initialize()
    await database.create_project(Project(id=PROJECT, name="p"))
    yield database
    await database.close()


@pytest.fixture
def ctx(db, dsn, tmp_path):
    config = AppConfig(
        database=DatabaseConfig(url=dsn),
        workspace_dir=str(tmp_path / "workspaces"),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.bus = EventBus(env="dev")
    yield DoctorContext(config=config, db=db, handler=CommandHandler(orch, config))


def vault_root_of(config) -> Path:
    return Path(config.vault_root)


def vault_rel(rid: str) -> str:
    return f"projects/{PROJECT}/specs/2026-09-21-review-{rid}.md"


async def new_review(db, *, state="in_review", with_gate=True, rid=None, body=DOC) -> str:
    """Insert a review row + first revision + (optional) gate, in one transaction.

    Mirrors what ``ReviewService.submit`` would have produced.
    """
    rid = rid or (await db.generate_review_id())
    rel = vault_rel(rid)
    gate_id = None
    if with_gate:
        gate_id, _ = await db.create_gate(
            PROJECT, "review", f"review {rid}", question="review", await_id=rid
        )
    async with db.immediate() as conn:
        await db.insert_review(
            review={
                "id": rid,
                "project_id": PROJECT,
                "author_task_id": "author",
                "kind": "spec",
                "title": "Review",
                "vault_path": rel,
                "current_revision": 1,
                "state": state,
                "gate_id": gate_id,
                "decider": "user",
                "notified_revision": 0,
                "created_at": NOW,
                "updated_at": NOW,
            },
            revision={
                "review_id": rid,
                "revision": 1,
                "content": body,
                "content_sha256": body_sha256(body),
                "submitted_by": "session:worker",
                "submitted_task_id": None,
                "changes_note": None,
                "submitted_at": NOW,
            },
            conn=conn,
        )
    return rid


def write_vault_file(db_row: dict, root: Path, state: str | None = None, body: str = DOC) -> None:
    fm = _frontmatter_for(db_row)
    fm["status"] = state or db_row["state"]
    out = root / db_row["vault_path"]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(fm, body), encoding="utf-8")


def read_vault_file(root: Path, rel: str) -> tuple[dict, str]:
    text = (root / rel).read_bytes().decode("utf-8")
    fm_text, body = split_frontmatter(text)
    return dict(yaml.safe_load(fm_text)), body


# ---------------------------------------------------------------------------

async def test_registry_includes_reviews_consistency():
    reg = default_registry()
    assert CHECK_ID in reg.ids()


async def test_db_none_is_info():
    from src.config import AppConfig as _AC
    ctx = DoctorContext(config=_AC())
    result = await _check(ctx)
    assert result.severity == Severity.INFO


async def test_all_consistent_is_ok(ctx):
    db = ctx.db
    root = vault_root_of(ctx.config)
    rid = await new_review(db, state="in_review", with_gate=True)
    write_vault_file(await db.get_review(rid), root)
    result = await _check(ctx)
    assert result.severity == Severity.OK, result.detail
    assert not result.data


async def test_approved_with_gate_resolved_and_vault_ok_is_ok(ctx):
    db = ctx.db
    root = vault_root_of(ctx.config)
    rid = await new_review(db, state="approved", with_gate=True)
    gate_id = (await db.get_review(rid))["gate_id"]
    await db.resolve_gate(gate_id, resolved_by="op", resolution="approved")
    write_vault_file(await db.get_review(rid), root, state="approved")
    result = await _check(ctx)
    assert result.severity == Severity.OK, result.detail


async def test_gate_missing_reports_warn(ctx):
    db = ctx.db
    root = vault_root_of(ctx.config)
    rid = await new_review(db, state="in_review", with_gate=False)
    write_vault_file(await db.get_review(rid), root)
    result = await _check(ctx)
    assert result.severity == Severity.WARN
    assert rid in result.data.get("gate_missing", [])
    assert not result.fixable


async def test_gate_deleted_after_review_created_is_warn(ctx):
    db = ctx.db
    root = vault_root_of(ctx.config)
    rid = await new_review(db, state="in_review", with_gate=True)
    gate_id = (await db.get_review(rid))["gate_id"]
    write_vault_file(await db.get_review(rid), root)
    from sqlalchemy import delete

    from src.database.tables import gates
    async with db._engine.begin() as conn:
        await conn.execute(delete(gates).where(gates.c.id == gate_id))
    result = await _check(ctx)
    assert result.severity == Severity.WARN
    assert rid in result.data.get("gate_missing", [])


async def test_gate_resolved_while_review_still_in_review_is_warn(ctx):
    db = ctx.db
    root = vault_root_of(ctx.config)
    rid = await new_review(db, state="in_review", with_gate=True)
    gate_id = (await db.get_review(rid))["gate_id"]
    write_vault_file(await db.get_review(rid), root)
    await db.resolve_gate(gate_id, resolved_by="sweeper", resolution="stale")
    result = await _check(ctx)
    assert result.severity == Severity.WARN
    assert rid in result.data.get("gate_resolved_unapproved", [])
    assert not result.fixable


async def test_approved_review_with_gate_still_open_fix_resolves_gate(ctx):
    db = ctx.db
    root = vault_root_of(ctx.config)
    rid = await new_review(db, state="approved", with_gate=True)
    gate_id = (await db.get_review(rid))["gate_id"]
    write_vault_file(await db.get_review(rid), root, state="approved")

    result = await _check(ctx)
    assert result.severity == Severity.WARN
    assert rid in result.data.get("approved_gate_open", [])
    assert result.fixable

    fixed = await _fix(ctx)
    assert fixed.fix_applied
    assert rid in fixed.data.get("resolved", [])

    assert (await db.get_gate(gate_id))["status"] == "resolved"
    assert (await _check(ctx)).severity == Severity.OK


async def test_missing_vault_file_fix_rewrites_file(ctx):
    db = ctx.db
    root = vault_root_of(ctx.config)
    rid = await new_review(db, state="in_review", with_gate=True)
    # Simulate a missing file: the vault file was never written, or deleted.

    result = await _check(ctx)
    assert result.severity == Severity.WARN
    assert rid in result.data.get("vault_missing", [])
    assert result.fixable

    fixed = await _fix(ctx)
    assert fixed.fix_applied
    assert rid in fixed.data.get("rewritten", [])

    review = await db.get_review(rid)
    fm, body = read_vault_file(root, review["vault_path"])
    assert fm["status"] == review["state"]
    assert fm["review"] == rid
    assert fm["revision"] == review["current_revision"]
    assert body == DOC
    assert (await _check(ctx)).severity == Severity.OK


async def test_diverged_vault_file_is_reported_and_never_fixed(ctx):
    db = ctx.db
    root = vault_root_of(ctx.config)
    rid = await new_review(db, state="in_review", with_gate=True)
    review = await db.get_review(rid)
    write_vault_file(review, root, body="# Human edit\n")
    before = (root / review["vault_path"]).read_bytes()

    result = await _check(ctx)
    assert result.severity == Severity.WARN
    assert rid in result.data.get("diverged", [])
    assert not result.fixable

    await _fix(ctx)
    assert (root / review["vault_path"]).read_bytes() == before


async def test_withdrawn_review_with_waiters_is_reported(ctx):
    db = ctx.db
    root = vault_root_of(ctx.config)
    rid = await new_review(db, state="withdrawn", with_gate=True)
    gate_id = (await db.get_review(rid))["gate_id"]
    tid = f"t_{rid}"
    await db.create_task(
        Task(id=tid, project_id=PROJECT, title="w", description="x", status=TaskStatus.READY)
    )
    async with db.immediate() as conn:
        await db.attach_gate_waiters(gate_id, [tid], conn=conn)
    review = await db.get_review(rid)
    write_vault_file(review, root)

    result = await _check(ctx)
    assert result.severity == Severity.WARN
    assert result.data.get("withdrawn_waiters", {}).get(rid) == [tid]
    assert not result.fixable


async def test_fix_is_idempotent(ctx):
    """Running --fix twice on the same broken state makes the second a no-op."""
    db = ctx.db
    root = vault_root_of(ctx.config)
    rid = await new_review(db, state="approved", with_gate=True)
    write_vault_file(await db.get_review(rid), root, state="approved")

    first = await _fix(ctx)
    second = await _fix(ctx)
    assert first.fix_applied
    assert not second.fix_applied
    assert first.data.get("resolved") == [rid]
    assert "resolved" not in second.data
