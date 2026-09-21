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
  with the review and resolved only by an approval.  A rejection or a
  withdrawal leaves it open, so dependent work never starts on a design that
  was not approved.
"""

from __future__ import annotations

import datetime
import logging
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from src.database.tables import DOC_REVIEW_DECIDERS, DOC_REVIEW_KINDS
from src.reviews.diff import block_diff
from src.reviews.vault import (
    body_sha256,
    candidate_paths,
    render,
    split_frontmatter,
    write_atomic,
)

logger = logging.getLogger(__name__)

__all__ = ["MAX_CONTENT_BYTES", "ReviewError", "ReviewHooks", "ReviewService"]

#: Submitted content, frontmatter included, in UTF-8 bytes (256 KB).
MAX_CONTENT_BYTES = 262144
#: Comment bodies, in characters (the ``ck_doc_review_comments_body`` bound).
MAX_COMMENT_CHARS = 16000
#: Titles are one line: they become the vault slug and a frontmatter value.
MAX_TITLE_CHARS = 200

OPEN_STATES = frozenset({"in_review", "changes_requested"})
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
    #: ``(review row, feedback text)``: hand the feedback to the author (§5.3).
    changes_requested: Callable[[dict, str], Awaitable[None]]


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
    ) -> dict:
        """Create a review at revision 1, its gate and its vault file."""
        kind = _kind(kind)
        title = _title(title)
        if decider not in DOC_REVIEW_DECIDERS:
            raise ValueError(f"unknown decider {decider!r}")
        body = _body(content)
        now = self._clock()
        date = time.strftime("%Y-%m-%d", time.localtime(now))

        for attempt in range(1, _SUBMIT_ATTEMPTS + 1):
            review_id = await self.db.generate_review_id()
            try:
                async with self.db.immediate() as conn:
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
                            review_id, 1, body, submitted_by, author_task_id, None, now
                        ),
                        conn=conn,
                    )
                break
            except IntegrityError:
                if attempt == _SUBMIT_ATTEMPTS:
                    raise
                logger.info("review submit raced another submission; retrying", exc_info=True)

        self._sync_vault(review, body)
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
    ) -> dict:
        """Store revision N+1 and put the review back ``in_review``.

        *resolves* marks those open comments addressed in the new revision.
        A vault file edited outside the review is copied to
        ``<name>.edited-<unix>.md`` (returned as ``backup_path``) before the
        new revision replaces it.
        """
        review = await self._get(review_id)
        if review["state"] not in OPEN_STATES:
            raise self._closed(review)
        body = _body(content)
        current = review["current_revision"]
        previous = await self._get_revision(review_id, current)
        now = self._clock()
        new_revision = current + 1

        async with self.db.immediate() as conn:
            moved = await self.db.transition_review(
                review_id,
                from_states=set(OPEN_STATES),
                expected_revision=current,
                values={"state": "in_review", "current_revision": new_revision, "updated_at": now},
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
                    ),
                    conn=conn,
                )
                await self.db.resolve_review_comments(
                    review_id, list(resolves or []), new_revision, conn=conn
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
            self._sync_vault(review, body)
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
        self, *, review_id: str, revision: int, approve: bool, note: str, decided_by: str
    ) -> dict:
        """Approve (resolving the gate) or request changes on the current revision."""
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
        state = "approved" if approve else "changes_requested"
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
        if not moved:
            raise await self._lost_race(review_id)

        review = await self._get(review_id)
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
                    review, self.feedback_text(review, note, open_comments)
                )
            except Exception:
                logger.exception("review %s: handing feedback to the author failed", review_id)

        unblocked_ids = sorted(unblocked)
        await self._emit(
            "review.decided",
            {
                **_base_payload(review),
                "decision": "approve" if approve else "request_changes",
                "decided_by": decided_by,
                "note": note,
                "unblocked_task_ids": unblocked_ids,
            },
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

    async def withdraw(self, *, review_id: str, reason: str, by: str) -> dict:
        """Close the review unapproved; its gate stays open and waiters are flagged."""
        review = await self._get(review_id)
        if review["state"] not in OPEN_STATES:
            raise self._closed(review)
        current = review["current_revision"]
        async with self.db.immediate() as conn:
            moved = await self.db.transition_review(
                review_id,
                from_states=set(OPEN_STATES),
                expected_revision=current,
                values={"state": "withdrawn", "updated_at": self._clock()},
                conn=conn,
            )
        if not moved:
            raise await self._lost_race(review_id)

        review = await self._get(review_id)
        await self._rewrite_status(review)
        flagged = (
            sorted(await self.db.get_gate_waiters(review["gate_id"])) if review["gate_id"] else []
        )
        for task_id in flagged:
            await self.db.set_task_meta(task_id, "needs_attention", "review_withdrawn")
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
                        review_id, new_revision, body, by, None, IMPORT_NOTE, now
                    ),
                    conn=conn,
                )
        if not moved:
            raise await self._lost_race(review_id)

        review = await self._get(review_id)
        self._sync_vault(review, body)
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
            self._sync_vault(review, current["content"])

        out: dict = {
            "review": review,
            "revision": shown,
            "revisions": await self.db.list_review_revisions(review_id),
            "vault_state": state,
        }
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

    def _sync_vault(self, review: dict, body: str) -> None:
        """Write the vault copy of *review* at its current revision; never raises."""
        # The submission's local date, as in the path ``candidate_paths`` chose.
        created = datetime.date.fromisoformat(
            time.strftime("%Y-%m-%d", time.localtime(review["created_at"]))
        )
        frontmatter = {
            "title": review["title"],
            "status": review["state"],
            "kind": review["kind"],
            "review": review["id"],
            "revision": review["current_revision"],
            "project": review["project_id"],
            "author_task": review["author_task_id"] or "",
            "date": created,
        }
        try:
            write_atomic(self._path(review), render(frontmatter, body))
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
        self._sync_vault(review, current["content"])

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
    ) -> dict:
        return {
            "review_id": review_id,
            "revision": revision,
            "content": body,
            "content_sha256": body_sha256(body),
            "submitted_by": submitted_by,
            "submitted_task_id": submitted_task_id,
            "changes_note": changes_note,
            "submitted_at": now,
        }

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
