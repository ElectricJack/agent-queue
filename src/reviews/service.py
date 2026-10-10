"""Document review service (spec §3-§7, §10).

``ReviewService`` is the review's domain logic: plain async Python over the
database, the vault and three injected hooks.  The ``aq review`` command
mixin owns authorization and wiring; nothing here asks who the caller is.

* **The database is the source of truth.**  Every state change is one
  transaction, and every transition is a compare-and-set on the review's
  state and revision (``transition_review``), so two racing decisions, or a
  decision racing a resubmit, cannot both succeed.
* **The vault file is a copy** written after the commit.  A failed write
  never fails the operation: the review exists, and the next ``show`` (or
  the consistency check) rewrites a missing file.  A file whose body was
  edited outside the review (*diverged*, §7) is never overwritten except by
  ``revise``, which copies it aside first.
* **The review gate** (type ``review``, ``await_id`` = review id) is created
  with the review and resolved only by an approval. A rejection leaves it
  open; withdrawal cancels it and flags its waiters. Reopening restores the same
  gate; neither cancellation nor reopening grants approval.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.database.tables import (
    DOC_REVIEW_DECIDERS, DOC_REVIEW_KINDS, doc_review_revisions, doc_reviews, tasks,
)
from src.reviews.diff import block_diff
from src.reviews.vault import (
    body_sha256,
    candidate_paths,
    frontmatter_for,
    render,
    split_frontmatter,
    spec_kind_from_content,
    write_atomic,
)

logger = logging.getLogger(__name__)

__all__ = ["MAX_CONTENT_BYTES", "PlaybookPin", "ReviewError", "ReviewHooks", "ReviewService"]

#: Submitted content, frontmatter included, in UTF-8 bytes (256 KB).
MAX_CONTENT_BYTES = 262144
#: Comment bodies, in characters (the ``ck_doc_review_comments_body`` bound).
MAX_COMMENT_CHARS = 16000
#: Titles are one line: they become the vault slug and a frontmatter value.
MAX_TITLE_CHARS = 200

OPEN_STATES = frozenset({"in_review", "changes_requested", "rejected"})
CLOSED_STATES = frozenset({"approved", "withdrawn"})

IMPORT_NOTE = "Imported edits made directly in the vault"

#: ``delegate(to=…)`` -> the ``decider`` it sets.
_DELEGATE_TO = {"user": "user", "supervisor": "user_or_supervisor"}

#: Attempts at the submit transaction; a retry follows only a unique
#: violation (a concurrent submit took the same vault path or review id).
_SUBMIT_ATTEMPTS = 3


class ReviewError(Exception):
    """A refusal with a named ``code`` (spec §5.1) and a human message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ReviewHooks:
    """What the service needs from the daemon, injected by the command mixin."""

    #: Persist (``log_event``) and publish (``bus.emit``) an event.
    emit: Callable[[str, dict], Awaitable[None]]
    #: ``(gate_id, resolved_by, resolution)`` -> the task ids it unblocked.
    resolve_gate: Callable[[str, str, str], Awaitable[set[str]]]
    #: ``(review row, decided revision, feedback text)``: hand feedback to the author.
    changes_requested: Callable[[dict, dict, str], Awaitable[None]]


@dataclass(frozen=True)
class PlaybookPin:
    """The compiled Playbook V2 artifact one revision asks approval for.

    ``meta`` is the ``doc_review_revisions.playbook`` JSON (playbook id,
    artifact hash, source hash, scope, ``activate_on_approval``, ...) and
    ``artifact`` the exact canonical artifact text approval stores.  The
    command layer compiles it; the service only keeps it with the revision.
    """

    meta: dict
    artifact: str

    @classmethod
    def from_revision(cls, row: dict) -> PlaybookPin | None:
        meta, artifact = row.get("playbook"), row.get("playbook_artifact")
        if not meta or not artifact:
            return None
        return cls(meta=dict(meta), artifact=artifact)


def _body(content: str) -> str:
    """The revision body of *content*: frontmatter stripped, then validated."""
    try:
        size = len(content.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ReviewError("not_utf8", f"the document is not valid UTF-8: {exc.reason}") from None
    if size > MAX_CONTENT_BYTES:
        raise ReviewError(
            "too_large",
            f"the document is {size} bytes; the limit is {MAX_CONTENT_BYTES} bytes of UTF-8",
        )
    _, body = split_frontmatter(content)
    if not body.strip():
        raise ReviewError("empty", "the document has no content outside its frontmatter")
    return body


def _spec_kind(content: str, default: str | None = None) -> str | None:
    try:
        return spec_kind_from_content(content, default)
    except ValueError as exc:
        raise ReviewError("bad_spec_kind", str(exc)) from None


def _title(title: str) -> str:
    cleaned = (title or "").strip()
    if not cleaned:
        raise ReviewError("bad_title", "a title is required")
    if len(cleaned) > MAX_TITLE_CHARS:
        raise ReviewError("bad_title", f"the title is longer than {MAX_TITLE_CHARS} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
        raise ReviewError("bad_title", "the title must be one line of text")
    return cleaned


def _kind(kind: str) -> str:
    if kind not in DOC_REVIEW_KINDS:
        raise ReviewError(
            "bad_kind", f"kind must be one of {', '.join(DOC_REVIEW_KINDS)}, not {kind!r}"
        )
    return kind


def _base_payload(review: dict) -> dict:
    return {
        "review_id": review["id"],
        "project_id": review["project_id"],
        "title": review["title"],
        "kind": review["kind"],
        "revision": review["current_revision"],
        "author_task_id": review["author_task_id"],
        "decider": review["decider"],
    }


class ReviewService:
    """Submit, revise, decide, comment on, withdraw and show document reviews."""

    def __init__(
        self,
        db,
        vault_root: str | Path,
        hooks: ReviewHooks,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.vault_root = Path(vault_root)
        self.hooks = hooks
        self._clock = clock

    # -- submit ------------------------------------------------------------

    async def submit(
        self,
        *,
        project_id: str,
        author_task_id: str | None,
        kind: str,
        title: str,
        content: str,
        submitted_by: str,
        decider: str = "user",
        playbook: PlaybookPin | None = None,
        single_author: bool = False,
    ) -> dict:
        """Create a review at revision 1, its gate and its vault file.

        *playbook* pins the compiled artifact this revision asks approval for.
        """
        kind = _kind(kind)
        title = _title(title)
        if decider not in DOC_REVIEW_DECIDERS:
            raise ValueError(f"unknown decider {decider!r}")
        body = _body(content)
        spec_kind = _spec_kind(content)
        now = self._clock()
        date = time.strftime("%Y-%m-%d", time.localtime(now))

        for attempt in range(1, _SUBMIT_ATTEMPTS + 1):
            review_id = await self.db.generate_review_id()
            try:
                async with self.db.immediate() as conn:
                    if single_author:
                        # The finalizer is the one result producer per attempt.
                        # Lock its task before checking, so concurrent retries
                        # cannot create two human result documents.
                        await conn.execute(select(tasks.c.id).where(
                            tasks.c.id == author_task_id,
                        ).with_for_update())
                        previous = (await conn.execute(select(doc_reviews).where(
                            doc_reviews.c.author_task_id == author_task_id,
                        ).order_by(doc_reviews.c.created_at).limit(1))).mappings().first()
                        if previous:
                            revision = (await conn.execute(select(
                                doc_review_revisions.c.content_sha256,
                                doc_review_revisions.c.spec_kind,
                            ).where(
                                doc_review_revisions.c.review_id == previous.id,
                                doc_review_revisions.c.revision == previous.current_revision,
                            ))).mappings().one()
                            if (revision.content_sha256 != body_sha256(body)
                                    or revision.spec_kind != spec_kind or previous.title != title
                                    or previous.kind != kind):
                                raise ReviewError(
                                    "result_exists",
                                    "This attempt already has a result; revise that review.",
                                )
                            await self._rewrite_status(dict(previous))
                            return {"review_id": previous.id, "vault_path": previous.vault_path,
                                    "revision": previous.current_revision, "gate_id": previous.gate_id}
                    vault_path = await self._free_path(project_id, kind, title, date, conn)
                    gate_id, _ = await self.db.create_gate(
                        project_id,
                        "review",
                        title,
                        question=f"Review {kind}: {title}",
                        await_id=review_id,
                        conn=conn,
                    )
                    review = {
                        "id": review_id,
                        "project_id": project_id,
                        "author_task_id": author_task_id,
                        "kind": kind,
                        "title": title,
                        "vault_path": vault_path,
                        "current_revision": 1,
                        "state": "in_review",
                        "gate_id": gate_id,
                        "decider": decider,
                        "created_at": now,
                        "updated_at": now,
                    }
                    await self.db.insert_review(
                        review=review,
                        revision=self._revision_row(
                            review_id, 1, body, submitted_by, author_task_id, None, now,
                            playbook, spec_kind,
                        ),
                        conn=conn,
                    )
                break
            except IntegrityError:
                if attempt == _SUBMIT_ATTEMPTS:
                    raise
                logger.info("review submit raced another submission; retrying", exc_info=True)

        self._sync_vault(review, body, playbook.meta if playbook else None, spec_kind)
        await self._emit("review.submitted", {**_base_payload(review), "vault_path": vault_path})
        return {"review_id": review_id, "vault_path": vault_path, "revision": 1, "gate_id": gate_id}

    async def _free_path(self, project_id: str, kind: str, title: str, date: str, conn) -> str:
        """The first candidate path no file and no review already holds."""
        for candidate in candidate_paths(project_id, kind, title, date):
            if (self.vault_root / candidate).exists():
                continue
            if await self.db.review_vault_path_taken(candidate, conn=conn):
                continue
            return candidate
        raise AssertionError("candidate_paths is infinite")  # pragma: no cover

    # -- revise ------------------------------------------------------------

    async def revise(
        self,
        *,
        review_id: str,
        content: str,
        changes_note: str,
        resolves: list[str],
        submitted_by: str,
        submitted_task_id: str | None,
        playbook: PlaybookPin | None = None,
        reopen: bool = False,
        expected_revision: int | None = None,
    ) -> dict:
        """Store revision N+1 and put the review back ``in_review``.

        *resolves* marks those open comments addressed in the new revision;
        *playbook* pins the artifact the new revision asks approval for.
        A vault file edited outside the review is copied to
        ``<name>.edited-<unix>.md`` (returned as ``backup_path``) before the
        new revision replaces it.
        """
        review = await self._get(review_id)
        allowed = {"withdrawn"} if reopen else set(OPEN_STATES)
        if review["state"] not in allowed:
            raise self._closed(review)
        body = _body(content)
        current = review["current_revision"]
        if expected_revision is not None and current != expected_revision:
            raise ReviewError("stale_revision", "review changed: reload it before reopening")
        previous = await self._get_revision(review_id, current)
        spec_kind = _spec_kind(content, previous.get("spec_kind"))
        now = self._clock()
        new_revision = current + 1

        async with self.db.immediate() as conn:
            moved = await self.db.transition_review(
                review_id,
                from_states=allowed,
                expected_revision=current,
                values={
                    "state": "in_review", "current_revision": new_revision, "updated_at": now,
                    **({"decided_by": None, "decided_at": None, "decision_note": None}
                       if reopen else {}),
                },
                conn=conn,
            )
            if moved:
                await self.db.insert_review_revision(
                    revision=self._revision_row(
                        review_id,
                        new_revision,
                        body,
                        submitted_by,
                        submitted_task_id,
                        changes_note,
                        now,
                        playbook,
                        spec_kind,
                    ),
                    conn=conn,
                )
                await self.db.resolve_review_comments(
                    review_id, list(resolves or []), new_revision, conn=conn
                )
                if reopen:
                    await self.db.set_review_gate_state(
                        review_id, review["gate_id"], cancelled=False, by=submitted_by, conn=conn,
                    )
        if not moved:
            raise await self._lost_race(review_id)

        review = await self._get(review_id)
        backup_path: str | None = None
        writable = True
        if self._read_state(review, previous["content_sha256"], body_sha256(body)) == "diverged":
            backup_path = self._backup(review, now)
            writable = backup_path is not None
        if writable:
            self._sync_vault(review, body, playbook.meta if playbook else None, spec_kind)
        else:
            logger.warning(
                "review %s: vault file %s was edited outside the review and could not be "
                "backed up; left as it is",
                review_id,
                review["vault_path"],
            )
        await self._emit(
            "review.revised",
            {
                **_base_payload(review),
                "changes_note": changes_note,
                "vault_path": review["vault_path"],
            },
        )
        return {
            "review_id": review_id,
            "revision": new_revision,
            "vault_path": review["vault_path"],
            "backup_path": backup_path,
        }

    def _backup(self, review: dict, now: float) -> str | None:
        """Copy the diverged vault file aside; its vault-relative path, or ``None``."""
        source = self._path(review)
        stem = review["vault_path"].removesuffix(".md")
        n = 1
        while True:
            suffix = f".edited-{int(now)}" + (f"-{n}" if n > 1 else "")
            relative = f"{stem}{suffix}.md"
            if not (self.vault_root / relative).exists():
                break
            n += 1
        try:
            shutil.copy2(source, self.vault_root / relative)
        except OSError:
            logger.warning("review %s: backing up %s failed", review["id"], source, exc_info=True)
            return None
        return relative

    # -- decide ------------------------------------------------------------

    async def decide(
        self, *, review_id: str, revision: int, decision: str, note: str, decided_by: str,
        responder_class: str | None = None, responder_profile: str | None = None,
        responder_profile_source: str | None = None,
    ) -> dict:
        """Approve or send feedback on the current revision."""
        if decision not in {"approve", "request_changes", "reject"}:
            raise ReviewError("not_in_review", "invalid review decision")
        approve = decision == "approve"
        review = await self._get(review_id)
        if review["state"] in CLOSED_STATES:
            raise self._closed(review)
        if review["state"] != "in_review":
            raise ReviewError(
                "not_in_review",
                f"review {review_id} is {review['state']}: wait for the author's next revision",
            )
        if revision != review["current_revision"]:
            raise ReviewError(
                "stale_revision",
                f"review {review_id} is at revision {review['current_revision']}, not "
                f"{revision}: reload it and decide on the current revision",
            )
        current = await self._get_revision(review_id, revision)
        if self.vault_state(review, current["content_sha256"]) == "diverged":
            raise ReviewError(
                "vault_diverged",
                f"the vault file {review['vault_path']} was edited outside the review: import "
                f"the edits (aq review import-edits --review-id {review_id}) or undo them first",
            )

        note = (note or "").strip()
        state = {"approve": "approved", "request_changes": "changes_requested", "reject": "rejected"}[decision]
        now = self._clock()
        async with self.db.immediate() as conn:
            moved = await self.db.transition_review(
                review_id,
                from_states={"in_review"},
                expected_revision=revision,
                values={
                    "state": state,
                    "decided_by": decided_by,
                    "decided_at": now,
                    "decision_note": note or None,
                    "updated_at": now,
                },
                conn=conn,
            )
            if moved and not approve:
                await self.db.set_review_revision_responder(
                    review_id, revision,
                    responder={
                        "responder_class": responder_class,
                        "responder_profile": responder_profile,
                        "responder_profile_source": responder_profile_source or "router",
                    },
                    conn=conn,
                )
        if not moved:
            raise await self._lost_race(review_id)

        review = await self._get(review_id)
        current = await self._get_revision(review_id, revision)
        unblocked: set[str] = set()
        if approve:
            # The decision is committed; a failed gate resolution is left for
            # the ``reviews.consistency`` check (§10), not reported as a
            # failed decision.
            try:
                unblocked = set(
                    await self.hooks.resolve_gate(review["gate_id"], decided_by, "approved")
                )
            except Exception:
                logger.exception(
                    "review %s: resolving gate %s failed", review_id, review["gate_id"]
                )
            await self._rewrite_status(review, current)
        else:
            await self._rewrite_status(review, current)
            open_comments = sum(
                1
                for c in await self.db.list_review_comments(review_id)
                if c["resolved_in_revision"] is None
            )
            try:
                await self.hooks.changes_requested(
                    review, current, self.feedback_text(review, note, open_comments)
                )
            except Exception:
                logger.exception("review %s: handing feedback to the author failed", review_id)

        unblocked_ids = sorted(unblocked)
        await self._emit(
            "review.decided",
            {
                **_base_payload(review),
                "decision": decision,
                "decided_by": decided_by,
                "note": note,
                "responder_class": current["responder_class"] if not approve else None,
                "responder_profile": current["responder_profile"] if not approve else None,
                "responder_profile_source": (
                    current["responder_profile_source"] if not approve else None
                ),
                "unblocked_task_ids": unblocked_ids,
            },
        )
        if approve and review["kind"] in {"spec", "plan"}:
            await self._emit(
                "spec.approved",
                {"project_id": review["project_id"], "spec_path": str(self._path(review).resolve())},
            )
        return {"review_id": review_id, "state": state, "unblocked_task_ids": unblocked_ids}

    # -- comment -----------------------------------------------------------

    async def comment(
        self,
        *,
        review_id: str,
        revision: int,
        quote: str | None,
        heading_path: list[str],
        body: str,
        author: str,
    ) -> dict:
        """Anchor a comment to *quote* (or a whole section) of one revision."""
        review = await self._get(review_id)
        if review["state"] in CLOSED_STATES:
            raise self._closed(review)
        if not body or not body.strip():
            raise ReviewError("empty", "a comment needs a body")
        if len(body) > MAX_COMMENT_CHARS:
            raise ReviewError("too_large", f"a comment is at most {MAX_COMMENT_CHARS} characters")
        if not 1 <= revision <= review["current_revision"]:
            raise ReviewError("not_found", f"review {review_id} has no revision {revision}")
        comment_id = f"cmt-{uuid.uuid4().hex[:12]}"
        await self.db.insert_review_comment(
            {
                "id": comment_id,
                "review_id": review_id,
                "revision": revision,
                "quote": quote or None,
                "heading_path": [str(h) for h in heading_path or []],
                "body": body,
                "author": author,
                "resolved_in_revision": None,
                "created_at": self._clock(),
            }
        )
        await self._emit(
            "review.commented",
            {**_base_payload(review), "revision": revision, "comment_id": comment_id},
        )
        return {"comment_id": comment_id}

    # -- withdraw ------------------------------------------------------------

    async def reopen(
        self, *, review_id: str, revision: int, by: str, submitted_task_id: str | None = None,
    ) -> dict:
        """Copy the withdrawn revision into N+1 and reopen its existing gate."""
        previous = await self._get_revision(review_id, revision)
        return await self.revise(
            review_id=review_id, content=previous["content"], changes_note="Reopened withdrawn review",
            resolves=[], submitted_by=by, submitted_task_id=submitted_task_id,
            playbook=PlaybookPin.from_revision(previous), reopen=True, expected_revision=revision,
        )

    async def withdraw(self, *, review_id: str, reason: str, by: str, via: str = "cli") -> dict:
        """Cancel the gate without approval and flag its dependent tasks atomically.

        Audit fields survive reopening. While withdrawn, the decision fields
        also carry ``by`` and ``reason`` for older readers.
        """
        review = await self._get(review_id)
        if review["state"] not in OPEN_STATES:
            raise self._closed(review)
        current = review["current_revision"]
        reason = (reason or "").strip()
        now = self._clock()
        async with self.db.immediate() as conn:
            moved = await self.db.transition_review(
                review_id,
                from_states=set(OPEN_STATES),
                expected_revision=current,
                values={
                    "state": "withdrawn",
                    "decided_by": by,
                    "decided_at": now,
                    "decision_note": reason or None,
                    "withdrawn_by": by,
                    "withdrawn_at": now,
                    "withdrawn_via": via,
                    "withdrawal_reason": reason or None,
                    "updated_at": now,
                },
                conn=conn,
            )
            if moved:
                flagged = await self.db.set_review_gate_state(
                    review_id, review["gate_id"], cancelled=True, by=by, conn=conn,
                )
        if not moved:
            raise await self._lost_race(review_id)

        review = await self._get(review_id)
        await self._rewrite_status(review)
        for task_id in flagged:
            task = await self.db.get_task(task_id)
            await self._emit(
                "task.needs_attention",
                {
                    "task_id": task_id,
                    "project_id": task.project_id if task else review["project_id"],
                    "title": task.title if task else task_id,
                    "reason": "review_withdrawn",
                    "review_id": review_id,
                },
            )
        await self._emit(
            "review.withdrawn",
            {
                **_base_payload(review),
                "reason": reason,
                "withdrawn_by": by,
                "withdrawn_via": via,
                "withdrawn_at": now,
                "gate_id": review["gate_id"],
                "gate_status": "cancelled",
                "flagged_task_ids": flagged,
            },
        )
        return {"review_id": review_id, "flagged_task_ids": flagged}

    # -- delegate ------------------------------------------------------------

    async def delegate(self, *, review_id: str, to: str) -> dict:
        """Let the supervisor decide this review too (``to="supervisor"``), or only Jack."""
        if to not in _DELEGATE_TO:
            raise ValueError(f"delegate to must be one of {sorted(_DELEGATE_TO)}, not {to!r}")
        decider = _DELEGATE_TO[to]
        # Delegation does not care which revision is current, so a
        # concurrent resubmit is retried rather than reported.
        for _ in range(3):
            review = await self._get(review_id)
            if review["state"] not in OPEN_STATES:
                raise self._closed(review)
            async with self.db.immediate() as conn:
                moved = await self.db.transition_review(
                    review_id,
                    from_states={review["state"]},
                    expected_revision=review["current_revision"],
                    values={"decider": decider, "updated_at": self._clock()},
                    conn=conn,
                )
            if moved:
                return {"review_id": review_id, "decider": decider}
        raise await self._lost_race(review_id)

    # -- import edits --------------------------------------------------------

    async def import_edits(self, *, review_id: str, by: str) -> dict:
        """Store the vault file's out-of-band body edit as revision N+1 (§7)."""
        review = await self._get(review_id)
        if review["state"] in CLOSED_STATES:
            raise self._closed(review)
        if review["state"] != "in_review":
            raise ReviewError(
                "not_in_review",
                f"review {review_id} is {review['state']}: only a review in review takes "
                "imported edits",
            )
        current = review["current_revision"]
        stored = await self._get_revision(review_id, current)
        if self.vault_state(review, stored["content_sha256"]) != "diverged":
            raise ReviewError("not_found", "the vault file has no local edits")
        try:
            text = self._path(review).read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            raise ReviewError(
                "not_utf8", f"the vault file {review['vault_path']} is not valid UTF-8"
            ) from None
        body = _body(text)
        spec_kind = _spec_kind(text, stored.get("spec_kind"))
        # The artifact is compiled from the playbook source, not this prose:
        # an edit to the document keeps the revision's pin.
        playbook = PlaybookPin.from_revision(stored)
        new_revision = current + 1
        now = self._clock()
        async with self.db.immediate() as conn:
            moved = await self.db.transition_review(
                review_id,
                from_states={"in_review"},
                expected_revision=current,
                values={"current_revision": new_revision, "updated_at": now},
                conn=conn,
            )
            if moved:
                await self.db.insert_review_revision(
                    revision=self._revision_row(
                        review_id, new_revision, body, by, None, IMPORT_NOTE, now, playbook,
                        spec_kind,
                    ),
                    conn=conn,
                )
        if not moved:
            raise await self._lost_race(review_id)

        review = await self._get(review_id)
        self._sync_vault(review, body, playbook.meta if playbook else None, spec_kind)
        await self._emit(
            "review.revised",
            {
                **_base_payload(review),
                "changes_note": IMPORT_NOTE,
                "vault_path": review["vault_path"],
            },
        )
        return {"review_id": review_id, "revision": new_revision}

    # -- show ----------------------------------------------------------------

    async def show(
        self,
        *,
        review_id: str,
        revision: int | None = None,
        comments: bool = False,
        diff_from: int | None = None,
    ) -> dict:
        """The review, one revision's content, the revision list and the vault state.

        ``vault_state`` is always judged against the *current* revision, and
        a missing vault file is rewritten from it.
        """
        review = await self._get(review_id)
        current_number = review["current_revision"]
        current = await self._get_revision(review_id, current_number)
        shown = current if revision in (None, current_number) else None
        if shown is None:
            shown = await self._get_revision(review_id, revision)

        state = self.vault_state(review, current["content_sha256"])
        if state == "missing":
            self._sync_vault(
                review, current["content"], current.get("playbook"), current.get("spec_kind"),
            )

        out: dict = {
            "review": review,
            # The pinned artifact bytes stay in the database; ``playbook``
            # names their hash.
            "revision": {k: v for k, v in shown.items() if k != "playbook_artifact"},
            "revisions": await self.db.list_review_revisions(review_id),
            "vault_state": state,
            "dependent_task_ids": (
                sorted(await self.db.get_gate_waiters(review["gate_id"]))
                if review["gate_id"] else []
            ),
            "gate": await self.db.get_gate(review["gate_id"]) if review["gate_id"] else None,
        }
        dispatches = await self.db.list_review_dispatches(review_id)
        for dispatch in dispatches:
            task = await self.db.get_task(dispatch["task_id"])
            if task is not None:
                dispatch["task_state"] = task.status.value
                # The router picks a dispatched reviewer's profile after the
                # dispatch is written (mandatory-routing spec §5.3).
                dispatch["routed_profile_id"] = task.profile_id
            else:
                archived = await self.db.get_archived_task(dispatch["task_id"])
                dispatch["task_state"] = archived["status"] if archived else "missing"
                dispatch["routed_profile_id"] = archived.get("profile_id") if archived else None
        out["dispatches"] = dispatches
        if comments:
            out["comments"] = await self.db.list_review_comments(review_id)
        if diff_from is not None:
            base = await self._get_revision(review_id, diff_from)
            out["diff"] = block_diff(base["content"], shown["content"])
        return out

    # -- the vault file ------------------------------------------------------

    def vault_state(self, review: dict, current_sha256: str) -> str:
        """``"ok"``, ``"missing"``, or ``"diverged"`` when the body differs (§7).

        Only the body counts: an edit to the frontmatter alone is not a
        divergence.  A file that cannot be read as UTF-8 text is diverged,
        so it is never overwritten.
        """
        path = self._path(review)
        try:
            text = path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            return "missing"
        except (OSError, UnicodeDecodeError):
            if not path.exists() and not path.is_symlink():
                return "missing"
            return "diverged"
        _, body = split_frontmatter(text)
        return "ok" if body_sha256(body) == current_sha256 else "diverged"

    def _read_state(self, review: dict, expected_sha256: str, incoming_sha256: str) -> str:
        """``vault_state`` against *expected*, but a file that already holds the
        incoming body is ``ok`` — there is nothing to back up."""
        state = self.vault_state(review, expected_sha256)
        if state == "diverged" and self.vault_state(review, incoming_sha256) == "ok":
            return "ok"
        return state

    def _path(self, review: dict) -> Path:
        return self.vault_root / review["vault_path"]

    def _sync_vault(
        self, review: dict, body: str, playbook: dict | None = None, spec_kind: str | None = None,
    ) -> None:
        """Write the vault copy of *review* at its current revision; never raises."""
        try:
            write_atomic(
                self._path(review), render(frontmatter_for(review, playbook, spec_kind), body),
            )
        except OSError:
            logger.warning(
                "review %s: writing vault file %s failed; the database copy stands",
                review["id"],
                review["vault_path"],
                exc_info=True,
            )

    async def _rewrite_status(self, review: dict, current: dict | None = None) -> None:
        """Rewrite the frontmatter after a state change, unless the file diverged."""
        if current is None:
            current = await self._get_revision(review["id"], review["current_revision"])
        if self.vault_state(review, current["content_sha256"]) == "diverged":
            logger.warning(
                "review %s is now %s, but vault file %s was edited outside the review; "
                "its frontmatter was left as it is",
                review["id"],
                review["state"],
                review["vault_path"],
            )
            return
        self._sync_vault(
            review, current["content"], current.get("playbook"), current.get("spec_kind"),
        )

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def feedback_text(review: dict, note: str, open_comments: int) -> str:
        """The feedback handed to the author when changes are requested (§5.3)."""
        rid, rev = review["id"], review["current_revision"]
        lines = [
            f"Review {rid} (revision {rev}) needs changes.",
            "",
            (note or "").strip() or "(no overall note)",
            "",
            f"{open_comments} open comment(s). Read them, with the text each one quotes:",
            f"  aq review show --review-id {rid} --comments",
            "Revise your draft, then resubmit:",
            (
                f"  aq review submit --review-id {rid} --file <draft.md> --changes "
                '"<what changed>" [--resolves <comment-id> ...]'
            ),
        ]
        return "\n".join(lines)

    @staticmethod
    def _revision_row(
        review_id: str,
        revision: int,
        body: str,
        submitted_by: str,
        submitted_task_id: str | None,
        changes_note: str | None,
        now: float,
        playbook: PlaybookPin | None = None,
        spec_kind: str | None = None,
    ) -> dict:
        row = {
            "review_id": review_id,
            "revision": revision,
            "content": body,
            "content_sha256": body_sha256(body),
            "spec_kind": spec_kind,
            "submitted_by": submitted_by,
            "submitted_task_id": submitted_task_id,
            "changes_note": changes_note,
            "submitted_at": now,
        }
        if playbook is not None:
            row["playbook"] = playbook.meta
            row["playbook_artifact"] = playbook.artifact
        return row

    async def _get(self, review_id: str) -> dict:
        review = await self.db.get_review(review_id)
        if review is None:
            raise ReviewError("not_found", f"no review {review_id}")
        return review

    async def _get_revision(self, review_id: str, revision: int) -> dict:
        row = await self.db.get_review_revision(review_id, revision)
        if row is None:
            raise ReviewError("not_found", f"review {review_id} has no revision {revision}")
        return row

    @staticmethod
    def _closed(review: dict) -> ReviewError:
        return ReviewError("review_closed", f"review {review['id']} is {review['state']}")

    async def _lost_race(self, review_id: str) -> ReviewError:
        """The refusal for a compare-and-set another request won."""
        review = await self.db.get_review(review_id)
        if review is not None and review["state"] in CLOSED_STATES:
            return self._closed(review)
        return ReviewError(
            "stale_revision",
            f"review {review_id} changed while this request ran: reload it and try again",
        )

    async def _emit(self, event_type: str, payload: dict) -> None:
        """Emit through the hook; the change is committed, so a failure only logs."""
        try:
            await self.hooks.emit(event_type, payload)
        except Exception:
            logger.warning("review event %s failed", event_type, exc_info=True)
