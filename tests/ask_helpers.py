"""Real PostgreSQL fixtures and a minimal locked source adapter for ask foundation tests."""

import hashlib
from dataclasses import replace

import pytest

from src.asks.models import (
    AskChoice,
    AskError,
    DecisionActor,
    SourceIdentity,
    SourceSnapshot,
    digest,
)
from src.asks.adapters import AdapterRegistry
from src.asks.service import AskService
from src.database import Database
from src.models import Project
from tests.db_fixtures import lease_dsn


@pytest.fixture(name="db")
async def db():
    database = Database(lease_dsn("asks.db"))
    await database.initialize()
    await database.create_project(Project(id="p-ask", name="Ask project"))
    async with database._engine.begin() as conn:
        await database.insert_review(
            review={
                "id": "rev-ask",
                "project_id": "p-ask",
                "kind": "spec",
                "title": "Review design",
                "vault_path": "projects/p-ask/specs/design.md",
                "state": "in_review",
                "created_at": 1,
                "updated_at": 1,
            },
            revision={
                "review_id": "rev-ask",
                "revision": 1,
                "content": "# Design\n",
                "content_sha256": hashlib.sha256(b"# Design\n").hexdigest(),
                "submitted_by": "user:jack",
                "submitted_at": 1,
            },
            conn=conn,
        )
    yield database
    await database.close()


def snapshot(key="rev-ask", kind="review"):
    return SourceSnapshot(
        SourceIdentity("p-ask", kind, key, {"id": key}),
        "source-v1",
        "revision-1",
        {
            "title": "Approve the retry design?",
            "why": "Implementation needs an agreed retry policy.",
            "waiting_effect": "Implementation stays held until approval.",
            "blocked_work": [],
            "blocked_work_note": "No tasks are waiting yet",
            "requested_by": {"label": "Project supervisor", "origin": "supervisor:p-ask"},
            "links": [{"label": "Read design", "url": "/reviews/rev-ask"}],
        },
        {"principals": ["user:jack"]},
        "supervisor:p-ask",
        {"principal": "user:jack"},
        (
            AskChoice("approve", "Approve", "Approve this exact document and its feedback packet."),
            AskChoice("decline", "Decline", "Keep dependent work held."),
        ),
    )


class LockedReviewAdapter:
    source_kind = "review"
    policy = ""
    version = "1"

    def __init__(self, database):
        self.db = database
        self.current = snapshot()
        self.lock_order = []

    async def snapshot(self, source, *, conn, lock=True):
        assert lock
        self.lock_order.append("source")
        row = await self.db.lock_review_for_mutation(source.key, conn)
        if row is None:
            raise AskError("ask.unsupported_source", "Review does not exist")
        return replace(
            self.current,
            feedback_version=row["feedback_version"],
            fingerprint=digest(
                {"revision": row["current_revision"], "feedback": row["feedback_version"]}
            ),
            generation_token=str(row["current_revision"]),
        )

    def choices(self, source):
        return source.choices

    async def validate(self, source, actor, intent, *, conn, accepted_decision=None):
        if actor.principal_id != source.decider["principal"]:
            raise AskError("ask.not_decider", "Not this source's decider")
        revision = await self.db.get_review_revision(source.identity.key, 1, conn=conn)
        return {
            "review_id": source.identity.key,
            "review_revision": 1,
            "content_sha256": revision["content_sha256"],
        }


@pytest.fixture(name="service")
def service(db):
    return AskService(db, AdapterRegistry([LockedReviewAdapter(db)]), clock=lambda: 1000)


def actor():
    return DecisionActor("user:jack", {"transport": "local", "authenticated": "operator"})


async def evidence(db, ask, *, conn, eid="evidence-1"):
    await db.insert_ask_correspondence(
        {
            "id": eid,
            "ask_id": ask["id"],
            "direction": "inbound",
            "transport": "local",
            "verified_actor": "user:jack",
            "text": "Approve",
            "entry_kind": "decision_evidence",
            "received_at": 1000,
            "created_at": 1000,
        },
        conn=conn,
    )
    return eid
