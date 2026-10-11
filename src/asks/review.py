"""Review adapter: source-first serialization, authenticated authority and frozen feedback."""

from __future__ import annotations

import re
from dataclasses import asdict

from sqlalchemy import select

from src.asks.models import (
    AskChoice,
    AskError,
    DecisionActor,
    SourceSnapshot,
    digest,
)
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.database.tables import ask_decisions, doc_review_attachments, sessions, task_gates

CHOICES = (
    AskChoice(
        "approve", "Approve", "Approve this revision after every feedback item has owned work."
    ),
    AskChoice(
        "request_changes",
        "Request changes",
        "Return a revision for final approval; keep work held.",
    ),
    AskChoice(
        "fold_in", "Fold in feedback", "Revise the document and return it for final approval."
    ),
    AskChoice("decline", "Decline", "Record a decline and keep approval-dependent work held."),
    AskChoice(
        "reject",
        "Reject and revise",
        "Legacy rejection: create a revision task and keep work held.",
    ),
)
DISPOSITIONS = {"follow_up", "fold_in", "applied", "no_change", "withdrawn"}
FINAL_DISPOSITIONS = {"applied", "no_change", "withdrawn"}


def authenticated_actor() -> DecisionActor:
    """Construct identity only from the command's authenticated execution context."""
    principal = current_principal() or TRUSTED_LOCAL
    if principal.kind is PrincipalKind.LOCAL:
        return DecisionActor("human:local-operator", {"kind": "local", "authenticated": "operator"})
    if principal.kind is PrincipalKind.SESSION:
        return DecisionActor(
            f"session:{principal.session_id}",
            {
                "kind": "session",
                "session_id": principal.session_id,
                "project_id": principal.project_id,
                "instance_token": principal.session_instance_token,
            },
        )
    raise AskError("ask.not_decider", "A human or explicitly delegated supervisor is required")


async def authorize_review(review, actor, *, conn):
    principal = current_principal() or TRUSTED_LOCAL
    if actor != authenticated_actor():
        raise AskError("ask.not_decider", "Decision actor differs from authenticated principal")
    if principal.kind is PrincipalKind.LOCAL:
        return
    if principal.project_id not in (None, review["project_id"]):
        raise AskError("ask.not_found", "Request was not found")
    row = (
        (
            await conn.execute(
                select(sessions).where(
                    sessions.c.id == principal.session_id,
                )
            )
        )
        .mappings()
        .first()
    )
    if not (
        principal.elevated
        and review["decider"] in {"supervisor", "user_or_supervisor"}
        and row
        and row["profile_id"] == "supervisor"
        and row["lifecycle"] == "named"
        and row["state"] in {"starting", "running", "draining"}
        and row["desired_state"] == "running"
        and row["instance_token"] == principal.session_instance_token
        and row["project_id"] in (None, review["project_id"])
    ):
        raise AskError(
            "ask.not_decider", "Only the human or a live delegated supervisor may decide"
        )


async def authorize_application(review, decision, *, conn):
    """Recheck delegated authority while the accepted decision is still pending."""
    if review["pending_decision_id"] != decision["id"]:
        return
    actor = decision["actor"]
    if actor["provenance"]["kind"] == "local":
        return  # immutable correspondence binds the authenticated human acceptance
    provenance = actor["provenance"]
    row = (
        (
            await conn.execute(
                select(sessions).where(
                    sessions.c.id == provenance.get("session_id"),
                )
            )
        )
        .mappings()
        .first()
    )
    if not (
        review["decider"] in {"supervisor", "user_or_supervisor"}
        and row
        and row["profile_id"] == "supervisor"
        and row["lifecycle"] == "named"
        and row["state"] in {"starting", "running", "draining"}
        and row["desired_state"] == "running"
        and row["instance_token"] == provenance.get("instance_token")
        and row["project_id"] in (None, review["project_id"])
    ):
        raise AskError(
            "ask.not_decider",
            "Accepted delegation is no longer current; reconcile this held decision",
        )


class ReviewAdapter:
    source_kind = "review"
    policy = ""
    version = "1"

    def __init__(self, review_service):
        self.service = review_service
        self.db = review_service.db

    async def snapshot(self, source, *, conn, lock=True):
        review = await self.db.lock_review_for_mutation(source.key, conn)
        if (
            not review
            or review["project_id"] != source.project_id
            or source.reference != {"id": source.key}
        ):
            raise AskError("ask.not_found", "Request was not found")
        revision = await self.db.get_review_revision(
            source.key, review["current_revision"], conn=conn
        )
        attachments = await self.attachments(source.key, conn=conn)
        waiters = list(
            (
                await conn.execute(
                    select(task_gates.c.task_id)
                    .where(
                        task_gates.c.gate_id == review["gate_id"],
                    )
                    .order_by(task_gates.c.task_id)
                )
            ).scalars()
        )
        generation = {
            "revision": review["current_revision"],
            "content": revision["content_sha256"],
            "generation": review["decision_generation"],
            "decider": review["decider"],
            "waiters": waiters,
        }
        fingerprint = digest(
            {
                **generation,
                "state": review["state"],
                "pending": review["pending_decision_id"],
                "feedback": review["feedback_version"],
                "attachments": attachments,
            }
        )
        pending = review["pending_decision_id"] is not None
        state = {"rejected": "changes_requested", "in_review": "pending"}.get(
            review["state"], review["state"]
        )
        if review["state"] == "rejected":
            accepted = (
                await conn.execute(
                    select(ask_decisions.c.packet)
                    .where(
                        ask_decisions.c.review_id == review["id"],
                        ask_decisions.c.review_revision == review["current_revision"],
                    )
                    .order_by(ask_decisions.c.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if accepted and accepted["domain"]["effective_option"] == "decline":
                state = "declined"
        return SourceSnapshot(
            source,
            fingerprint,
            digest(generation),
            {
                "title": review["title"],
                "why": f"Decide on revision {review['current_revision']} of this {review['kind']} document.",
                "waiting_effect": "Approval-dependent work stays held until this decision is applied.",
                "blocked_work": [{"label": tid, "url": f"/tasks/{tid}"} for tid in waiters[:50]],
                "blocked_work_note": f"{len(waiters)} task(s) wait on the review gate.",
                "requested_by": {"label": revision["submitted_by"], "origin": "review submission"},
                "links": [{"label": "Read and discuss", "url": f"/reviews/{source.key}"}],
            },
            {"project_id": source.project_id},
            f"supervisor:{source.project_id}",
            {"review_decider": review["decider"]},
            CHOICES,
            review["feedback_version"],
            next_actor="agent" if pending else "human" if state == "pending" else "none",
            decision_state=state,
            application_state="applying" if pending else "not_started",
        )

    def choices(self, snapshot):
        return snapshot.choices

    async def attachments(self, review_id, *, conn):
        rows = (
            await conn.execute(
                select(doc_review_attachments)
                .where(
                    doc_review_attachments.c.review_id == review_id,
                )
                .order_by(doc_review_attachments.c.revision, doc_review_attachments.c.id)
            )
        ).mappings()
        return [{k: v for k, v in row.items() if k != "path"} for row in rows]

    async def validate(self, snapshot, actor, intent, *, conn, accepted_decision=None):
        # snapshot acquired the review lock before any ask lock; rereading is safe.
        from src.database.tables import doc_reviews

        review = dict(
            (
                await conn.execute(
                    select(doc_reviews).where(
                        doc_reviews.c.id == snapshot.identity.key,
                    )
                )
            )
            .mappings()
            .one()
        )
        await authorize_review(review, actor, conn=conn)
        if accepted_decision is not None:
            return accepted_decision["packet"]["domain"]
        if review["pending_decision_id"]:
            raise AskError(
                "ask.stale_source", "Decision pending; reconcile before amending the review"
            )
        if review["state"] != "in_review":
            raise AskError("review_closed", "Return a revision or create an amendment review")
        revision = await self.db.get_review_revision(
            review["id"],
            review["current_revision"],
            conn=conn,
        )
        if self.service.vault_state(review, revision["content_sha256"]) == "diverged":
            raise AskError(
                "vault_diverged", "Import or undo the external vault edits before deciding"
            )
        comments = await self.db.list_review_comments(review["id"], conn=conn)
        attachments = await self.attachments(review["id"], conn=conn)
        items = [
            {
                "feedback_key": c["id"],
                "comment_id": c["id"],
                "original_text": c["body"],
                "original_author": c["author"],
                "original_revision": c["revision"],
                "original_anchor": {"quote": c["quote"], "heading_path": c["heading_path"]},
            }
            for c in comments
        ]
        if intent.overall_note:
            items.append(
                {
                    "feedback_key": "overall_note",
                    "comment_id": None,
                    "original_text": intent.overall_note,
                    "original_author": actor.principal_id,
                    "original_revision": review["current_revision"],
                    "original_anchor": {},
                }
            )
        selected = {}
        for disposition in intent.dispositions:
            if set(disposition) - {
                "feedback_key",
                "disposition",
                "reason",
                "required_before_release",
                "changes_document",
                "external_prerequisite",
                "coverage",
            }:
                raise AskError("ask.context_missing", "Unknown disposition field")
            if any(
                type(disposition[k]) is not bool
                for k in ("required_before_release", "changes_document", "external_prerequisite")
                if k in disposition
            ):
                raise AskError("ask.context_missing", "Disposition conditions must be booleans")
            key = disposition.get("feedback_key")
            if key in selected or key not in {i["feedback_key"] for i in items}:
                raise AskError(
                    "ask.stale_feedback", "Disposition must name one existing feedback item"
                )
            if disposition.get("disposition") not in DISPOSITIONS:
                raise AskError("ask.context_missing", "Unknown feedback disposition")
            coverage = disposition.get("coverage")
            if coverage is not None:
                if (not isinstance(coverage, dict) or set(coverage) - {"leaf_keys", "waiter_ids"}
                        or any(not isinstance(ids, list) or not ids
                               or any(not isinstance(i, str) or not i for i in ids)
                               or len(set(ids)) != len(ids) for ids in coverage.values())):
                    raise AskError("ask.context_missing", "Coverage must name explicit leaf/waiter identities")
            if (disposition.get("external_prerequisite") or coverage is not None) and (
                disposition.get("disposition") != "follow_up"
                or not disposition.get("required_before_release")
                or not disposition.get("external_prerequisite")
                or disposition.get("changes_document")
                or not disposition.get("reason")
                or revision.get("spec_kind") != "implementation"
            ):
                raise AskError("ask.context_missing", "External coverage requires explicit accepted implementation prerequisite")
            selected[key] = disposition
        fold = intent.option_key in {"request_changes", "fold_in", "reject"}
        for item in items:
            choice = selected.get(item["feedback_key"], {})
            body = item["original_text"]
            human = item["original_author"].startswith(("human:", "user:"))
            # Unclassified agent findings are held for review. Human prose is mandatory
            # work by default; explicit correctness/conditional language requires revision.
            prerequisite = bool(
                re.search(
                    r"(?i)\[blocking\]|\b(critical|high severity|prerequisite)\b|"
                    r"\b(if|unless|must|correctness|security|deadlock|race condition|provided that|"
                    r"before approv|change the (?:spec|document))",
                    body,
                )
            ) or (not human and not re.search(r"(?i)\[(nit|nonblocking|low|medium)\]", body))
            disposition = choice.get("disposition", "fold_in" if prerequisite else "follow_up")
            if choice.get("required_before_release") or choice.get("changes_document"):
                prerequisite = True
            if disposition in FINAL_DISPOSITIONS:
                if not choice.get("reason"):
                    raise AskError(
                        "ask.context_missing", "Accepting no work requires a decider reason"
                    )
                if disposition == "withdrawn" and actor.provenance.get("kind") != "local":
                    raise AskError(
                        "ask.not_decider", "Withdrawing human feedback requires human provenance"
                    )
            elif prerequisite and not choice.get("external_prerequisite"):
                disposition = "fold_in"
            fold |= disposition == "fold_in"
            item.update(
                disposition=disposition,
                required_before_release=prerequisite,
                acceptance_text=body,
                accepted_by={"actor": asdict(actor), "choice": choice},
            )
        effective = "fold_in" if fold and intent.option_key == "approve" else intent.option_key
        # A decline holds the source and does not commission revision work.
        return {
            "review_id": review["id"],
            "review_revision": review["current_revision"],
            "review": review,
            "content_sha256": revision["content_sha256"],
            "spec_kind": revision.get("spec_kind"),
            "decider": review["decider"],
            "playbook": revision.get("playbook"),
            "attachments": attachments,
            "attachment_manifest_sha256": digest(attachments),
            "comments": comments,
            "feedback": items,
            "effective_option": effective,
            "task_kind": "revision"
            if effective in {"fold_in", "request_changes", "reject"}
            else "followup"
            if effective == "approve"
            and any(i["disposition"] not in FINAL_DISPOSITIONS for i in items)
            else None,
            "original_verified_input": {
                **asdict(actor),
                "text": intent.overall_note,
                "option_key": intent.option_key,
            },
            "overall_note": intent.overall_note,
            "responder_class": intent.responder_class,
        }

    async def reserve_on(self, decision, *, conn):
        from src.asks.operations import reserve_review_operations

        await reserve_review_operations(self.db, decision, conn=conn)
