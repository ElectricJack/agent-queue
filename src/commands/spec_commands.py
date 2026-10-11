"""Spec lifecycle commands (design §8, Phase 6).

``spec_approve`` snapshots standalone body bytes and authenticates a local
approval in one transaction. Frontmatter is only a mirror; a durable decision
dispatch lets the default pipeline commission its ingestion role task.
"""
from __future__ import annotations

import hashlib
import io
import time
from dataclasses import asdict
import logging
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from sqlalchemy import select, text

from src.asks.adapters import AdapterRegistry
from src.asks.ingestion import dispatch
from src.asks.models import AskError, DecisionIntent, SourceIdentity, digest
from src.asks.review import authenticated_actor
from src.asks.service import AskService, version_of
from src.asks.standalone import StandaloneSpecAdapter
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.database.tables import ask_decisions, doc_reviews
from src.reviews.vault import spec_kind_from_content

logger = logging.getLogger(__name__)


def _make_ruamel() -> YAML:
    """Round-trip YAML that preserves comments and key order."""
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096  # avoid line wrapping surprises
    return y


class SpecCommandsMixin:
    """Registers ``spec_approve``."""

    async def _cmd_spec_approve(self, args: dict) -> dict:
        if (current_principal() or TRUSTED_LOCAL).kind is not PrincipalKind.LOCAL:
            return {"success": False, "error_code": "ask.not_decider",
                    "error": "Standalone approval requires the local operator"}
        if set(args) - {"project_id", "spec_path"}:
            return {"success": False, "error_code": "ask.not_decider",
                    "error": "Approval authority is server-owned"}
        project_id = args.get("project_id") or self._active_project_id
        spec_path_raw = args.get("spec_path")
        if not project_id:
            return {"success": False, "error": "project_id is required"}
        if not spec_path_raw:
            return {"success": False, "error": "spec_path is required"}
        if not str(spec_path_raw).endswith(".md"):
            return {"success": False, "error": "spec path must end in .md"}

        vault_root = Path(self.config.vault_root).resolve()
        allowed_root = (vault_root / "projects" / project_id / "specs").resolve()
        spec_path = Path(spec_path_raw).resolve()
        try:
            spec_path.relative_to(allowed_root)
        except ValueError:
            return {
                "success": False,
                "error": f"spec_path is outside {allowed_root}",
            }
        if not spec_path.is_file():
            return {"success": False, "error": f"spec not found: {spec_path}"}

        raw = spec_path.read_bytes().decode("utf-8")
        if not raw.startswith("---"):
            return {
                "success": False,
                "error": "spec is missing YAML frontmatter",
            }
        parts = raw.split("---", 2)
        if len(parts) < 3:
            return {"success": False, "error": "spec frontmatter is malformed"}
        # Round-trip via ruamel so comments and key order in the
        # frontmatter survive the status flip.
        rt = _make_ruamel()
        try:
            fm = rt.load(parts[1]) or {}
        except YAMLError as exc:
            return {
                "success": False,
                "error": f"frontmatter YAML error: {exc}",
            }

        if not isinstance(fm, dict):
            return {"success": False, "error": "spec frontmatter must be a mapping"}
        async with self.db._engine.connect() as conn:
            reviewed = (await conn.execute(select(doc_reviews.c.id).where(
                doc_reviews.c.project_id == project_id,
                doc_reviews.c.vault_path == str(spec_path.relative_to(vault_root)),
            ))).scalar_one_or_none()
        if fm.get("review") or reviewed:
            return {
                "success": False,
                "error_code": "review_gate",
                "error": (
                    "reviewed specs are decided in the review: "
                    f"aq review decide --review-id {reviewed or fm['review']} "
                    "--revision <n> --decision approve"
                ),
            }

        actor = authenticated_actor()
        body = parts[2].encode("utf-8")
        spec_kind = spec_kind_from_content(raw, default="design")
        snapshot_id = "approved-spec-" + digest([project_id, str(spec_path),
                                               hashlib.sha256(body).hexdigest(), spec_kind])
        service = AskService(self.db, AdapterRegistry([StandaloneSpecAdapter(self.db)]))
        source = SourceIdentity(project_id, "standalone_spec", snapshot_id,
                                {"snapshot_id": snapshot_id})
        try:
            async with self.db.immediate() as conn:
                await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                                   {"key": snapshot_id})
                snapshot = await self.db.get_approved_document_snapshot(snapshot_id, conn=conn)
                if not snapshot:
                    await self.db.insert_approved_document_snapshot({
                        "id": snapshot_id, "project_id": project_id, "source_path": str(spec_path),
                        "title": str(fm.get("title") or spec_path.stem)[:200], "body": body,
                        "body_sha256": hashlib.sha256(body).hexdigest(), "spec_kind": spec_kind,
                        "approval_provenance": asdict(actor), "created_at": time.time(),
                    }, conn=conn)
                existing = (await conn.execute(select(ask_decisions).where(
                    ask_decisions.c.snapshot_id == snapshot_id,
                ))).mappings().first()
                if existing:
                    decision = dict(existing)
                else:
                    publication = await service.publish(source, conn=conn)
                    intent = DecisionIntent("approve", version_of(publication.ask))
                    preview = await service.prepare_decision(source, actor, intent, conn=conn)
                    evidence_id = "spec-input-" + digest(snapshot_id)
                    await self.db.insert_ask_correspondence({
                        "id": evidence_id, "ask_id": publication.ask["id"],
                        "direction": "inbound", "transport": "local",
                        "verified_actor": actor.principal_id, "text": "approve",
                        "entry_kind": "decision_evidence", "received_at": time.time(),
                        "created_at": time.time(),
                    }, conn=conn)
                    decision, _ = await service.decide(
                        source, actor, intent, conn=conn, preview_id=preview["id"],
                        idempotency_key=snapshot_id, evidence_entry_id=evidence_id,
                    )
        except (AskError, ValueError) as exc:
            return {"success": False, "error_code": getattr(exc, "code", "spec.invalid"),
                    "error": str(exc)}
        # The mirror is presentation only. Never replace bytes changed since acceptance.
        if spec_path.read_bytes().decode("utf-8") != raw:
            return {"success": False, "decision_id": decision["id"],
                    "error": "Approved packet saved; mirror changed and needs reconciliation"}
        fm["status"] = "approved"
        buf = io.StringIO()
        rt.dump(fm, buf)
        new_fm = buf.getvalue().rstrip("\n")
        spec_path.write_text(f"---\n{new_fm}\n---{parts[2]}", encoding="utf-8")

        bus = getattr(self.orchestrator, "bus", None)
        if bus is not None:
            try:
                await dispatch(self.db, decision, bus.emit, self.config.vault_root)
            except Exception:
                logger.warning("Approved spec dispatch remains pending", exc_info=True)
        return {"success": True, "decision_id": decision["id"],
                "packet_sha256": decision["packet_sha256"], "snapshot_id": snapshot_id}
