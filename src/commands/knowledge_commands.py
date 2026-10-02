"""Worker-safe knowledge and record command mixins (K03).

Mechanism only: authorization, activation gating, concurrency and idempotency
all live in the record services. These handlers bind the session principal,
route to the right service, and translate ``RecordError`` into a transport
result. Policy lives in profiles and playbooks, never here.
"""

from __future__ import annotations

from src.commands.principal import current_principal
from src.knowledge.service import EDIT_FIELDS
from src.records.models import RecordError


def _error(code, message):
    return {"success": False, "error_code": code, "error": str(message)}


class KnowledgeCommandsMixin:
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
                principal=principal,
                project_id=args.get("project_id"),
                idempotency_key=args.get("idempotency_key"),
                claim_epoch=args.get("claim_epoch"),
            )
        except RecordError as exc:
            return exc.result()

    async def _search(self, args, *, query=""):
        return await self._knowledge_service().search(
            principal=current_principal(),
            project_id=args.get("project_id"),
            query=query,
            category=args.get("category"),
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
                project_id=args.get("project_id"),
                revision_id=args.get("revision_id"),
            )
        except RecordError as exc:
            return exc.result()

    # -- knowledge: update / history / diff ----------------------------------

    async def _cmd_knowledge_update(self, args):
        patch = {key: value for key, value in args.items() if key in EDIT_FIELDS and value is not None}
        try:
            return await self._knowledge_service().update(
                identity=args.get("identity"),
                patch=patch,
                principal=current_principal(),
                project_id=args.get("project_id"),
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
                project_id=args.get("project_id"),
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
                project_id=args.get("project_id"),
                revision_id=args.get("from_revision"),
            )
            after = await service.show(
                identity=identity,
                principal=principal,
                project_id=args.get("project_id"),
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
                project_id=args.get("project_id"),
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
                project_id=args.get("project_id"),
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
                project_id=args.get("project_id"),
                revision_id=args.get("revision_id"),
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_record_search(self, args):
        try:
            return await self._search(args, query=args.get("query") or "")
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
        return {
            "success": True,
            "outcome": "read",
            "capabilities": {
                "enabled": cfg.enabled,
                "global_enabled": cfg.global_enabled,
                "writes_enabled": cfg.writes_enabled,
                "ui_enabled": cfg.ui_enabled,
                "legacy_memory_mode": cfg.legacy_memory_mode,
                "features": {
                    name: getattr(cfg, name).enabled
                    for name in ("context", "semantic", "extraction", "consolidation", "export")
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
                project_id=args.get("project_id"),
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
                project_id=args.get("project_id"),
                revision_id=args.get("revision_id"),
            )
        except RecordError as exc:
            return exc.result()

    async def _cmd_link_remove(self, args):
        link_id = args.get("link_id")
        try:
            return await self._record_service().mutate_links(
                identity=args.get("identity"),
                operations=[{"action": "remove", "link_id": link_id}],
                principal=current_principal(),
                project_id=args.get("project_id"),
                idempotency_key=args.get("idempotency_key"),
                if_revision=args.get("if_revision"),
                if_link_token=args.get("if_link_token"),
                claim_epoch=args.get("claim_epoch"),
            )
        except RecordError as exc:
            return exc.result()
