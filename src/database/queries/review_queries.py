"""Document review queries — ``doc_reviews``, revisions and comments.

Implements the storage half of the document-review spec §3.2
(``vault/projects/agent-queue/specs/2026-09-21-document-review-design.md``).
The database is the review's source of truth; the vault file is a copy the
review service writes after each commit.

Methods that take ``conn`` run inside the caller's transaction and never
commit; the others open their own.  Rows are plain ``dict``s.  Task ids are
soft references (no foreign key to ``tasks``), so nothing here ever blocks a
task's archive or delete.
"""

from __future__ import annotations

import random

from sqlalchemy import and_, exists, insert, or_, select, update

from src.database.tables import (
    doc_review_comments, doc_review_dispatches, doc_review_revisions, doc_reviews, task_gates,
)
from src.task_names import ADJECTIVES, NOUNS

__all__ = ["ReviewQueriesMixin"]

#: Plain ``rev-<adjective>-<noun>`` picks before falling back to a numeric
#: suffix — the same budget as ``fresh_root_id`` in ``src/task_names.py``.
_ID_RETRIES = 10

#: ``list_review_revisions`` omits the (up to 256 KB) body.
_REVISION_SUMMARY = [c for c in doc_review_revisions.c if c.name != "content"]


class ReviewQueriesMixin:
    """Document review CRUD, compare-and-set transitions and id generation.

    Mixed into the database adapter alongside the other query mixins.
    Expects ``self._engine``.
    """

    # -- ids and paths ---------------------------------------------------

    async def generate_review_id(self) -> str:
        """A fresh ``rev-<adjective>-<noun>`` id no review already has."""
        async with self._engine.begin() as conn:
            for _ in range(_ID_RETRIES):
                review_id = f"rev-{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
                if not await _review_exists(conn, review_id):
                    return review_id
            while True:
                review_id = (
                    f"rev-{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}"
                    f"-{random.randint(10, 99)}"
                )
                if not await _review_exists(conn, review_id):
                    return review_id

    async def review_vault_path_taken(self, vault_path: str, *, conn) -> bool:
        """Whether a review already owns *vault_path* (relative to the vault root)."""
        row = (
            await conn.execute(
                select(doc_reviews.c.id).where(doc_reviews.c.vault_path == vault_path).limit(1)
            )
        ).first()
        return row is not None

    # -- reviews -----------------------------------------------------------

    async def insert_review(self, *, review: dict, revision: dict, conn) -> None:
        """Insert a review row and its first revision on the caller's transaction."""
        await conn.execute(insert(doc_reviews).values(**review))
        await conn.execute(insert(doc_review_revisions).values(**revision))

    async def get_review(self, review_id: str) -> dict | None:
        async with self._engine.begin() as conn:
            row = (
                (await conn.execute(select(doc_reviews).where(doc_reviews.c.id == review_id)))
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def lock_review_for_dispatch(self, review_id: str, *, conn) -> dict | None:
        """Serialize dispatches and hold the review state stable through task creation."""
        row = (
            await conn.execute(
                select(doc_reviews).where(doc_reviews.c.id == review_id).with_for_update()
            )
        ).mappings().first()
        return dict(row) if row else None

    async def list_review_dispatches(self, review_id: str, *, conn=None) -> list[dict]:
        stmt = (
            select(doc_review_dispatches)
            .where(doc_review_dispatches.c.review_id == review_id)
            .order_by(doc_review_dispatches.c.created_at, doc_review_dispatches.c.id)
        )
        if conn is not None:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        else:
            async with self._engine.begin() as owned:
                rows = (await owned.execute(stmt)).mappings().fetchall()
        return [dict(row) for row in rows]

    async def get_review_dispatch_for_task(self, task_id: str) -> dict | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(doc_review_dispatches).where(doc_review_dispatches.c.task_id == task_id)
                )
            ).mappings().first()
        return dict(row) if row else None

    async def insert_review_dispatch(self, dispatch: dict, *, conn) -> None:
        await conn.execute(insert(doc_review_dispatches).values(**dispatch))

    async def list_reviews(
        self,
        *,
        project_id: str | None = None,
        state: str | None = None,
        kind: str | None = None,
        task_id: str | None = None,
    ) -> list[dict]:
        """Reviews matching every given filter, most recently updated first.

        *task_id* matches a review the task authored, or one whose gate the
        task waits on (``task_gates``).  Each row then carries ``relation``:
        ``"author"`` or ``"waiting"`` (a task that is both is its author).
        """
        conditions = []
        if project_id is not None:
            conditions.append(doc_reviews.c.project_id == project_id)
        if state is not None:
            conditions.append(doc_reviews.c.state == state)
        if kind is not None:
            conditions.append(doc_reviews.c.kind == kind)
        if task_id is not None:
            waiting = exists().where(
                and_(
                    task_gates.c.gate_id == doc_reviews.c.gate_id,
                    task_gates.c.task_id == task_id,
                )
            )
            conditions.append(or_(doc_reviews.c.author_task_id == task_id, waiting))
        stmt = select(doc_reviews)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        stmt = stmt.order_by(doc_reviews.c.updated_at.desc(), doc_reviews.c.id.asc())
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        reviews = [dict(r) for r in rows]
        if task_id is not None:
            for review in reviews:
                review["relation"] = "author" if review["author_task_id"] == task_id else "waiting"
        return reviews

    async def transition_review(
        self,
        review_id: str,
        *,
        from_states: set[str],
        expected_revision: int,
        values: dict,
        conn,
    ) -> bool:
        """Apply *values* only while the review is in *from_states* at *expected_revision*.

        The compare-and-set every state change goes through: ``False`` means
        a concurrent decision, revision or withdrawal got there first, and
        nothing was written.
        """
        result = await conn.execute(
            update(doc_reviews)
            .where(
                and_(
                    doc_reviews.c.id == review_id,
                    doc_reviews.c.state.in_(sorted(from_states)),
                    doc_reviews.c.current_revision == expected_revision,
                )
            )
            .values(**values)
        )
        return result.rowcount == 1

    # -- revisions ---------------------------------------------------------

    async def insert_review_revision(self, *, revision: dict, conn) -> None:
        await conn.execute(insert(doc_review_revisions).values(**revision))

    async def set_review_revision_responder(
        self, review_id: str, revision: int, *, responder: dict, conn
    ) -> None:
        await conn.execute(
            update(doc_review_revisions)
            .where(
                doc_review_revisions.c.review_id == review_id,
                doc_review_revisions.c.revision == revision,
            )
            .values(**responder)
        )

    async def get_review_revision(self, review_id: str, revision: int, *, conn=None) -> dict | None:
        """One revision, with its content."""
        stmt = select(doc_review_revisions).where(
            and_(
                doc_review_revisions.c.review_id == review_id,
                doc_review_revisions.c.revision == revision,
            )
        )
        if conn is not None:
            row = (await conn.execute(stmt)).mappings().first()
        else:
            async with self._engine.begin() as owned:
                row = (await owned.execute(stmt)).mappings().first()
        return dict(row) if row else None

    async def list_review_revisions(self, review_id: str) -> list[dict]:
        """Every revision of *review_id*, oldest first, without content."""
        stmt = (
            select(*_REVISION_SUMMARY)
            .where(doc_review_revisions.c.review_id == review_id)
            .order_by(doc_review_revisions.c.revision.asc())
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [dict(r) for r in rows]

    # -- comments ----------------------------------------------------------

    async def insert_review_comment(self, comment: dict) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(insert(doc_review_comments).values(**comment))

    async def list_review_comments(self, review_id: str) -> list[dict]:
        """Every comment on *review_id*, in the order they were made."""
        stmt = (
            select(doc_review_comments)
            .where(doc_review_comments.c.review_id == review_id)
            .order_by(doc_review_comments.c.created_at.asc(), doc_review_comments.c.id.asc())
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [dict(r) for r in rows]

    async def resolve_review_comments(
        self, review_id: str, comment_ids: list[str], revision: int, *, conn
    ) -> int:
        """Mark the named, still-open comments of *review_id* resolved in *revision*.

        Ids that belong to another review, are already resolved or do not
        exist are ignored.  Returns how many comments were marked.
        """
        if not comment_ids:
            return 0
        result = await conn.execute(
            update(doc_review_comments)
            .where(
                and_(
                    doc_review_comments.c.review_id == review_id,
                    doc_review_comments.c.id.in_(sorted(set(comment_ids))),
                    doc_review_comments.c.resolved_in_revision.is_(None),
                )
            )
            .values(resolved_in_revision=revision)
        )
        return result.rowcount

    # -- Discord outbox (spec §9) ------------------------------------------

    async def reviews_pending_notification(self) -> list[dict]:
        """Reviews whose current revision has not been announced yet, oldest first."""
        stmt = (
            select(doc_reviews)
            .where(doc_reviews.c.notified_revision < doc_reviews.c.current_revision)
            .order_by(doc_reviews.c.updated_at.asc(), doc_reviews.c.id.asc())
        )
        async with self._engine.begin() as conn:
            rows = (await conn.execute(stmt)).mappings().fetchall()
        return [dict(r) for r in rows]

    async def mark_review_notified(self, review_id: str, revision: int) -> None:
        """Record *revision* as announced; never moves the outbox backwards."""
        async with self._engine.begin() as conn:
            await conn.execute(
                update(doc_reviews)
                .where(
                    and_(
                        doc_reviews.c.id == review_id,
                        doc_reviews.c.notified_revision < revision,
                    )
                )
                .values(notified_revision=revision)
            )


async def _review_exists(conn, review_id: str) -> bool:
    row = (
        await conn.execute(select(doc_reviews.c.id).where(doc_reviews.c.id == review_id).limit(1))
    ).first()
    return row is not None
