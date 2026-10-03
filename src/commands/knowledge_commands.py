"""Worker-safe knowledge and record command mixins (K03).

Mechanism only: authorization, activation gating, concurrency and idempotency
all live in the record services. These handlers bind the session principal,
route to the right service, and translate ``RecordError`` into a transport
result. Policy lives in profiles and playbooks, never here.
"""

from __future__ import annotations

from src.commands.principal import current_principal
from src.knowledge.models import EDIT_FIELDS
from src.records.models import RecordError


def _error(code, message):
    return {"success": False, "error_code": code, "error": str(message)}


def _knowledge_scope(args):
    from src.records.auth import GLOBAL_SCOPE

    if args.get("global_scope", False):
        if args.get("project_id"):
            raise RecordError("record.invalid_input", "Choose project_id or global_scope")
        return GLOBAL_SCOPE
    return args.get("project_id")


class KnowledgeCommandsMixin:
    async def _cmd_knowledge_create_task(self, args):
        from src.knowledge.task_creation import create_task_from_knowledge

        try:
            return await create_task_from_knowledge(self, args, current_principal())
        except RecordError as exc:
            return exc.result()

    async def _cmd_knowledge_export(self, args):
        from src.records.export import RecordExporter

        try:
            exporter = getattr(self, "_record_exporter_instance", None)
            if exporter is None:
                exporter = RecordExporter(
                    self.db, lambda: self.config.knowledge, lambda: self.config.vault_root
                )
                self._record_exporter_instance = exporter
            return await exporter.manual(
                identity=args.get("identity"),
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                revision_id=args.get("revision_id"),
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_record_repair(self, args):
        from src.commands.principal import PrincipalKind
        from src.records.backfill import TaskRecordBackfill
        from src.records.identity import RecordIntegrityError
        from src.records.models import uuid_value
        from src.records.outbox import RecordOutbox

        # An elevated supervisor is still not the local operator. Applying
        # backfill/replay is an explicit operator action, never a worker grant.
        principal = current_principal()
        if principal is None or principal.kind != PrincipalKind.LOCAL:
            return _error("record.forbidden", "Record repair requires the local operator")
        dry_run = args.get("dry_run", True)
        if not dry_run and not self.config.knowledge.enabled:
            return _error("knowledge.disabled", "Enable core records before applying repair")
        try:
            if args.get("operation") == "backfill-task-mappings":
                return await TaskRecordBackfill(self.db).run(
                    dry_run=dry_run, max_batches=args.get("max_batches", 2)
                )
            if args.get("operation") == "replay-outbox":
                return await RecordOutbox(self.db, self.config.knowledge).replay(
                    event_id=uuid_value(args.get("event_id"), "event_id"), dry_run=dry_run
                )
            return _error("record.invalid_input", "Unknown record repair operation")
        except RecordError as exc:
            return exc.result()
        except (RecordIntegrityError, ValueError) as exc:
            return _error("record.integrity_conflict", exc)

    def _knowledge_service(self):
        service = getattr(self, "_knowledge_service_instance", None)
        if service is None:
            from src.knowledge.service import KnowledgeService

            service = KnowledgeService(self.db, self.config.knowledge)
            self._knowledge_service_instance = service
        return self._knowledge_service_instance

    def _record_service(self):
        service = getattr(self, "_record_service_instance", None)
        if service is None:
            from src.records.service import RecordService

            service = RecordService(self.db, self.config.knowledge)
            self._record_service_instance = service
        return self._record_service_instance

    # -- knowledge: create / list / show -------------------------------------

    async def _cmd_knowledge_create(self, args):
        principal = current_principal()
        try:
            snapshot = {
                "title": args.get("title"),
                "body": args.get("body"),
                "category": args.get("category"),
                "summary": args.get("summary"),
                "tags": args.get("tags") or [],
                "sources": args.get("sources") or [],
                "metadata": args.get("metadata") or {},
            }
            return await self._knowledge_service().create(
                snapshot=snapshot,
                source_task_id=args.get("source_task_id"),
                if_link_token=args.get("if_link_token"),
                principal=principal,
                project_id=_knowledge_scope(args),
                idempotency_key=args.get("idempotency_key"),
                claim_epoch=args.get("claim_epoch"),
            )
        except RecordError as exc:
            return exc.result()

    async def _search(self, args, *, query="", operation="knowledge_list"):
        return await self._knowledge_service().search(
            principal=current_principal(),
            project_id=_knowledge_scope(args),
            query=query,
            operation=operation,
            category=args.get("category"),
            lifecycle=args.get("lifecycle"),
            verification=args.get("verification"),
            include_retired=bool(args.get("include_retired", False)),
            include_disputed=bool(args.get("include_disputed", False)),
            limit=int(args.get("limit", 25)),
            cursor=args.get("cursor"),
        )

    async def _cmd_knowledge_list(self, args):
        try:
            return await self._search(args)
        except RecordError as exc:
            return exc.result()

    async def _cmd_knowledge_show(self, args):
        try:
            return await self._knowledge_service().show(
                identity=args.get("identity"),
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                revision_id=args.get("revision_id"),
            )
        except RecordError as exc:
            return exc.result()

    # -- knowledge: update / history / diff ----------------------------------

    async def _cmd_knowledge_update(self, args):
        patch = {
            key: value for key, value in args.items() if key in EDIT_FIELDS and value is not None
        }
        try:
            return await self._knowledge_service().update(
                identity=args.get("identity"),
                patch=patch,
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                idempotency_key=args.get("idempotency_key"),
                if_revision=args.get("if_revision"),
                claim_epoch=args.get("claim_epoch"),
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_knowledge_history(self, args):
        try:
            return await self._knowledge_service().history(
                identity=args.get("identity"),
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                before_sequence=args.get("before_sequence"),
                limit=int(args.get("limit", 25)),
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_knowledge_diff(self, args):
        principal = current_principal()
        service = self._knowledge_service()
        identity = args.get("identity")
        try:
            before = await service.show(
                identity=identity,
                principal=principal,
                project_id=_knowledge_scope(args),
                revision_id=args.get("from_revision"),
            )
            after = await service.show(
                identity=identity,
                principal=principal,
                project_id=_knowledge_scope(args),
                revision_id=args.get("to_revision"),
            )
        except RecordError as exc:
            return exc.result()
        lhs = before.get("snapshot") or {}
        rhs = after.get("snapshot") or {}
        keys = sorted(set(lhs) | set(rhs))
        changes = [
            {"field": key, "from": lhs.get(key), "to": rhs.get(key)}
            for key in keys
            if lhs.get(key) != rhs.get(key)
        ]
        return {
            "success": True,
            "outcome": "read",
            "record_id": before.get("record_id"),
            "from_revision": before.get("revision_id"),
            "to_revision": after.get("revision_id"),
            "changes": changes,
        }

    # -- knowledge: retire / restore -----------------------------------------

    async def _cmd_knowledge_retire(self, args):
        try:
            return await self._knowledge_service().retire(
                identity=args.get("identity"),
                reason=args.get("reason", ""),
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                idempotency_key=args.get("idempotency_key"),
                if_revision=args.get("if_revision"),
                successor_record_id=args.get("successor_record_id"),
                claim_epoch=args.get("claim_epoch"),
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_knowledge_restore(self, args):
        try:
            return await self._knowledge_service().restore(
                identity=args.get("identity"),
                revision_id=args.get("revision_id"),
                reason=args.get("reason", ""),
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                idempotency_key=args.get("idempotency_key"),
                if_revision=args.get("if_revision"),
                claim_epoch=args.get("claim_epoch"),
            )
        except RecordError as exc:
            return exc.result()

    # -- records: show / search / capabilities --------------------------------

    async def _cmd_record_show(self, args):
        try:
            return await self._record_service().show(
                identity=args.get("identity"),
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                revision_id=args.get("revision_id"),
                include_edges=args.get("include_edges", False),
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_record_search(self, args):
        try:
            return await self._record_service().search(
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                kind=args.get("kind", "knowledge"),
                query=args.get("query") or "",
                category=args.get("category"),
                lifecycle=args.get("lifecycle"),
                verification=args.get("verification"),
                include_retired=args.get("include_retired", False),
                include_disputed=args.get("include_disputed", False),
                limit=args.get("limit", 25),
                cursor=args.get("cursor"),
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_record_capabilities(self, args):
        cfg = self.config.knowledge
        principal = current_principal()
        grants = set()
        if principal is not None and principal.policy is not None:
            grants = {
                name
                for name in principal.policy.aq_commands
                if name.startswith(("knowledge_", "record_", "link_"))
            }
        if principal is not None and principal.kind.value == "local":
            from src.commands.contracts.registry import CONTRACTS

            grants = {
                name
                for name in CONTRACTS.names()
                if name.startswith(("knowledge_", "record_", "link_"))
            }
        return {
            "success": True,
            "outcome": "read",
            "capabilities": {
                "enabled": cfg.enabled,
                "global_enabled": cfg.global_enabled,
                "writes_enabled": cfg.writes_enabled,
                "ui_enabled": cfg.ui_enabled,
                "enabled_projects": [
                    project
                    for project in cfg.enabled_projects
                    if principal is not None
                    and (
                        principal.kind.value == "local"
                        or principal.project_id == project
                        or (principal.elevated and principal.project_id is None)
                    )
                ],
                "legacy_memory_mode": cfg.legacy_memory_mode,
                "features": {
                    name: getattr(cfg, name).enabled
                    for name in (
                        "context",
                        "semantic",
                        "extraction",
                        "consolidation",
                        "export",
                        "import_inventory",
                        "import_apply",
                    )
                },
                "granted_operations": sorted(grants),
            },
        }

    # -- records: links -------------------------------------------------------

    async def _cmd_link_create(self, args):
        try:
            return await self._record_service().mutate_links(
                identity=args.get("identity"),
                operations=args.get("operations") or [],
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                idempotency_key=args.get("idempotency_key"),
                if_revision=args.get("if_revision"),
                if_link_token=args.get("if_link_token"),
                claim_epoch=args.get("claim_epoch"),
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_link_list(self, args):
        try:
            return await self._record_service().list_links(
                identity=args.get("identity"),
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                revision_id=args.get("revision_id"),
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_knowledge_import(self, args):
        """Inventory by default; explicit operator-only apply/resume/cancel."""
        from pathlib import Path

        from src.commands.principal import PrincipalKind
        from src.knowledge.imports.dry_run import run_dry_run
        from src.knowledge.imports.inventory import MalformedSource, RootSpec

        principal = current_principal()
        if principal is None or principal.kind != PrincipalKind.LOCAL:
            return _error("knowledge_import.forbidden", "Requires the local operator")
        cfg = self.config.knowledge
        operation = args.get("operation", "dry-run")
        if operation in {"apply", "resume", "cancel"}:
            from src.knowledge.imports.apply import ImportService

            try:
                service = ImportService(self.db, cfg, self.config.vault_root)
                common = dict(principal=principal, project_id=_knowledge_scope(args),
                              manifest_sha256=args.get("manifest_sha256"),
                              limit=args.get("limit", 100))
                if operation == "apply":
                    return await service.apply(
                        **common, manifest_content_base64=args.get("manifest_content_base64"),
                        selected_item_ids=args.get("selected_item_ids"),
                        expected_revisions=args.get("expected_revisions"),
                        expected_source_hashes=args.get("expected_source_hashes"),
                        idempotency_key=args.get("idempotency_key"),
                        backup_receipt=args.get("backup_receipt"),
                    )
                return await service.resume(**common, run_id=args.get("run_id"),
                                            cancel=operation == "cancel")
            except RecordError as exc:
                return exc.result()
            except (ValueError, OSError) as exc:
                return _error("knowledge_import.source_unavailable", str(exc))
        if operation != "dry-run":
            return _error("knowledge_import.invalid_input", "Unknown import operation")
        if not (cfg.enabled and getattr(cfg, "import_inventory", None).enabled):
            return _error(
                "knowledge_import.disabled",
                "Enable knowledge and knowledge.import_inventory before dry-run",
            )

        raw_roots = args.get("roots") or []
        try:
            roots = tuple(
                RootSpec(
                    root_id=str(spec["root_id"]),
                    path=Path(str(spec["path"])),
                    source_scope=str(spec["source_scope"]),
                    source_kind=str(spec.get("source_kind") or "memory"),
                    relative_paths=tuple(spec.get("relative_paths") or ()) or None,
                )
                for spec in raw_roots
            )
            report = await run_dry_run(
                roots=roots,
                vector_export=Path(args["vector_export"]) if args.get("vector_export") else None,
                scope_aliases=args.get("scope_aliases"),
                source_installation_id=str(args.get("source_installation_id") or ""),
                snapshot_id=str(args.get("snapshot_id") or ""),
                snapshot_timestamp=str(args.get("snapshot_timestamp") or ""),
            )
        except (MalformedSource, ValueError) as exc:
            return _error("knowledge_import.invalid_input", str(exc))
        except OSError as exc:
            return _error("knowledge_import.source_unavailable", str(exc))
        return report.to_dict()

    async def _cmd_link_remove(self, args):
        link_id = args.get("link_id")
        try:
            return await self._record_service().mutate_links(
                identity=args.get("identity"),
                operations=[{"action": "remove", "link_id": link_id}],
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                idempotency_key=args.get("idempotency_key"),
                if_revision=args.get("if_revision"),
                if_link_token=args.get("if_link_token"),
                claim_epoch=args.get("claim_epoch"),
            )
        except RecordError as exc:
            return exc.result()

    async def _protection(self, method, args):
        try:
            kwargs = {
                key: value
                for key, value in args.items()
                if key not in {"project_id", "global_scope"}
            }
            return await getattr(self._knowledge_service(), method)(
                principal=current_principal(),
                project_id=_knowledge_scope(args),
                **kwargs,
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_knowledge_propose(self, args):
        return await self._protection("propose", args)

    async def _cmd_knowledge_proposal_show(self, args):
        return await self._protection("proposal_show", args)

    async def _cmd_knowledge_proposal_decide(self, args):
        return await self._protection("proposal_decide", args)

    async def _cmd_knowledge_verify(self, args):
        return await self._protection("verify", args)

    async def _cmd_knowledge_authority_grant(self, args):
        return await self._protection("authority_grant", args)

    async def _cmd_knowledge_authority_revoke(self, args):
        return await self._protection("authority_revoke", args)

    async def _cmd_knowledge_share(self, args):
        return await self._protection("share", args)

    async def _cmd_knowledge_redact(self, args):
        return await self._protection("redact", args)
