"""Standalone document adapter. The local approval command snapshots bytes first."""

from src.asks.models import AskChoice, AskError, SourceSnapshot, digest
from src.asks.operations import get_operation, operation, succeed
from src.asks.review import authenticated_actor
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal


class StandaloneSpecAdapter:
    source_kind = "standalone_spec"
    policy = ""
    version = "1"

    def __init__(self, db):
        self.db = db

    async def snapshot(self, source, *, conn, lock=True):
        document = await self.db.get_approved_document_snapshot(source.key, conn=conn)
        if (not document or document["project_id"] != source.project_id
                or source.reference != {"snapshot_id": source.key}):
            raise AskError("ask.not_found", "Standalone snapshot was not found")
        return SourceSnapshot(
            source, document["body_sha256"], document["body_sha256"],
            {
                "title": document["title"],
                "why": "Approve this exact standalone document for spec ingestion.",
                "waiting_effect": "No derived work is commissioned until this approval is applied.",
                "blocked_work": [], "blocked_work_note": "No existing review waiters.",
                "requested_by": {"label": "Local operator", "origin": "spec_approve"},
                "links": [{"label": "Document mirror", "url": document["source_path"]}],
            },
            {"project_id": source.project_id}, f"supervisor:{source.project_id}",
            {"principal": "human:local-operator"},
            (AskChoice("approve", "Approve", "Ingest the immutable approved document."),),
        )

    def choices(self, snapshot):
        return snapshot.choices

    async def validate(self, snapshot, actor, intent, *, conn, accepted_decision=None):
        if ((current_principal() or TRUSTED_LOCAL).kind is not PrincipalKind.LOCAL
                or actor != authenticated_actor()):
            raise AskError("ask.not_decider", "Standalone approval requires the local operator")
        if accepted_decision:
            return accepted_decision["packet"]["domain"]
        if intent.option_key != "approve" or intent.dispositions or intent.overall_note:
            raise AskError("ask.context_missing", "Feedback requires a document review")
        document = await self.db.get_approved_document_snapshot(snapshot.identity.key, conn=conn)
        return {
            "snapshot_id": document["id"], "content_sha256": document["body_sha256"],
            "spec_kind": document["spec_kind"], "effective_option": "approve", "feedback": [],
            "attachment_manifest_sha256": digest([]),
        }

    async def reserve_on(self, decision, *, conn):
        applied = await get_operation(conn, decision["id"], "apply", lock=True)
        await succeed(conn, applied, {"decision_id": decision["id"], "state": "approved"})
        await operation(self.db, decision, "ingestion", conn=conn,
                        dependencies=[applied["operation_key"]])
        await self.db.update_ask_projection(decision["ask_id"], {
            "decision_state": "approved", "application_state": "applying", "next_actor": "agent",
        }, conn=conn)
