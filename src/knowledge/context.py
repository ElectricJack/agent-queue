"""Common, bounded context selection and execution-bound delivery ledger.

No retrieval provider is initialized here. All reads pass through core scope,
source and revision checks; preparation alone never records an injected citation.
"""

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.commands.principal import ExecutionPrincipal, PrincipalKind
from src.database.tables import (
    knowledge_citations,
    knowledge_context_bundles,
    knowledge_context_deliveries,
    projects,
    sessions,
    task_session_attempts,
    tasks,
)
from src.knowledge.authority import authority_on
from src.knowledge.budget import ContextBudget
from src.knowledge.models import (
    ContextBundle,
    ContextItem,
    content_hash,
    context_markdown,
    utc_text,
)
from src.knowledge.redaction import KNOWLEDGE_ERASURE_LOCK
from src.knowledge.search import lexical_search_on
from src.knowledge.service import KnowledgeService
from src.profiles.capabilities import capability_policy_for
from src.records.auth import GLOBAL_SCOPE, RecordAccess
from src.records.models import RecordError, uuid_value


def context_enabled(config):
    knowledge = getattr(config, "knowledge", None)
    return bool(getattr(knowledge, "enabled", False)
                and getattr(getattr(knowledge, "context", None), "enabled", False)
                and getattr(getattr(config, "memory", None), "enabled", False))


#: Refusals that mean *this caller may not read this scope* rather than *this
#: request was wrong*: the corpus is not available here, or the principal's
#: policy lacks the read grant. A delivery surface whose knowledge context is
#: an enrichment (prime, launch prompts) degrades on exactly these codes and
#: still renders its ordinary body; every other ``RecordError`` — a stale
#: claim, an over-budget prompt, an unavailable execution — is a real failure
#: of the read and keeps surfacing.
ACCESS_REFUSAL_CODES = frozenset({"record.forbidden", "knowledge.disabled"})


@dataclass(frozen=True)
class BootstrapPrincipal:
    """Daemon-only prelaunch identity; never accepted from command arguments."""

    principal: ExecutionPrincipal


def supervisor_bootstrap_principal(profile, *, project_id, session_id, instance_token):
    if not profile or profile.id != "supervisor" or not session_id or not instance_token:
        raise RecordError("record.forbidden")
    return BootstrapPrincipal(ExecutionPrincipal(
        kind=PrincipalKind.SERVICE, service_name="knowledge-context-bootstrap",
        policy=capability_policy_for(profile), profile_id=profile.id,
        project_id=project_id, session_id=session_id, session_instance_token=instance_token,
        elevated=True,
    ))


def live_bootstrap_principal(bootstrap):
    return replace(bootstrap.principal, kind=PrincipalKind.SESSION, service_name=None)


def principal_fingerprint(principal):
    # Bootstrap and the corresponding persisted session have the same identity.
    return content_hash(dict(
        session_id=principal.session_id, instance=principal.session_instance_token,
        profile_id=principal.profile_id, project_id=principal.project_id,
        task_id=principal.task_id, elevated=principal.elevated,
        commands=sorted(principal.policy.aq_commands),
    ))


class ContextService(KnowledgeService):
    def __init__(self, db, config):
        super().__init__(db, config.knowledge)
        self.app_config = config

    async def _owner(self, conn, principal, claim_epoch, *, bootstrap=False):
        if principal is None or not principal.session_id or not principal.session_instance_token:
            raise RecordError("context.execution_unavailable")
        row = (await conn.execute(select(sessions).where(
            sessions.c.id == principal.session_id,
        ).with_for_update(read=True))).mappings().first()
        instance = sha256(principal.session_instance_token.encode()).hexdigest()
        if bootstrap:
            if row is not None:
                raise RecordError("context.execution_unavailable")
            return dict(owner_kind="supervisor_session", owner_id=principal.session_id,
                        session_id=principal.session_id, session_instance=instance,
                        task_id=None, attempt_id=None, supervisor_session_id=principal.session_id,
                        claim_epoch=None)
        if (
            not row or row["state"] not in {"running", "draining"}
            or row["ended_at"] is not None
            or row["instance_token"] != principal.session_instance_token
            or row["profile_id"] != principal.profile_id
            or row["project_id"] != principal.project_id
            or principal.kind != PrincipalKind.SESSION
        ):
            raise RecordError("record.forbidden")
        if principal.elevated:
            if row["task_id"] is not None or row["lifecycle"] != "named":
                raise RecordError("context.execution_unavailable")
            return dict(owner_kind="supervisor_session", owner_id=row["id"],
                        session_id=row["id"], session_instance=instance,
                        task_id=None, attempt_id=None, supervisor_session_id=row["id"],
                        claim_epoch=None)
        held_task = principal.task_id or (row["task_id"] if row["lifecycle"] == "pool" else None)
        task = (await conn.execute(select(tasks).where(
            tasks.c.id == held_task,
        ).with_for_update(read=True))).mappings().first()
        if (
            not task or row["task_id"] != held_task
            or task["project_id"] != principal.project_id
            or task["status"] != "IN_PROGRESS" or not row["agent_id"]
            or task["assigned_agent_id"] != row["agent_id"]
            or type(claim_epoch) is not int or task["claim_epoch"] != claim_epoch
            or (row["lifecycle"] == "pool" and row["last_claim_epoch"] != claim_epoch)
        ):
            raise RecordError("record.stale_claim")
        attempt = await conn.scalar(select(task_session_attempts.c.id).where(
            task_session_attempts.c.session_id == row["id"],
            task_session_attempts.c.task_id == task["id"],
            task_session_attempts.c.ended_at.is_(None),
            task_session_attempts.c.state.in_(("starting", "running", "draining")),
        ).order_by(task_session_attempts.c.started_at.desc()).limit(1).with_for_update(read=True))
        if not attempt:
            raise RecordError("context.execution_unavailable")
        return dict(owner_kind="task_attempt", owner_id=attempt, session_id=row["id"],
                    session_instance=instance, task_id=task["id"], attempt_id=attempt,
                    supervisor_session_id=None, claim_epoch=claim_epoch)

    async def _scopes(self, conn, principal, project_ids, global_scope, *, bootstrap=False):
        if not context_enabled(self.app_config):
            raise RecordError("context.disabled")
        if (not isinstance(project_ids, (list, tuple)) or len(project_ids) > 8
                or any(not isinstance(p, str) or not p or p == "*" for p in project_ids)
                or type(global_scope) is not bool):
            raise RecordError("record.invalid_input", "Choose at most eight explicit projects")
        scopes = sorted(set(project_ids))
        if not scopes and not global_scope:
            if principal.project_id:
                scopes = [principal.project_id]
            else:
                # Global supervisors default to no private corpus.
                return []
        if global_scope:
            scopes.append(GLOBAL_SCOPE)
        accesses = []
        for scope in scopes:
            if bootstrap:
                if principal.project_id is not None and scope != principal.project_id:
                    raise RecordError("record.not_found")
                if scope is GLOBAL_SCOPE:
                    if (not self.config.global_enabled or principal.project_id is not None
                            or not principal.policy.allows_aq_command("knowledge_share")):
                        raise RecordError("record.not_found")
                elif (scope not in self.config.enabled_projects or not await conn.scalar(
                    select(projects.c.id).where(projects.c.id == scope)
                )):
                    raise RecordError("knowledge.disabled")
                accesses.append(RecordAccess(principal, f"session:{principal.session_id}",
                                             scope, None, self.config.global_enabled))
            else:
                accesses.append(await self._access(conn, principal, "knowledge_show", scope))
            if not principal.policy.allows_aq_command("knowledge_show"):
                raise RecordError("record.forbidden")
        return accesses

    async def _item(self, conn, access, identity, revision_id, reason, now):
        record = await self.resolve_on(identity, access, conn=conn)
        revision = await self._revision(record, revision_id, conn=conn)
        snapshot = await self._safe_snapshot(revision["snapshot"], access, conn=conn,
                                             scope_key=record["scope_key"])
        # A summary whose retained input vanished must not become evidence.
        if snapshot["summary_of_revision"]:
            await self._revision(record, snapshot["summary_of_revision"], conn=conn)
        freshness = "current"
        if snapshot["valid_from"] and datetime.fromisoformat(snapshot["valid_from"]) > now:
            freshness = "not_yet_valid"
        elif any(snapshot[k] and datetime.fromisoformat(snapshot[k]) < now
                 for k in ("valid_until", "recheck_at")):
            freshness = "stale"
        authority = await authority_on(conn, record, revision,
                                      review_required=self.config.authority_review_required)
        return ContextItem(
            record_id=str(record["record_id"]), revision_id=str(revision["revision_id"]),
            content_sha256=revision["content_sha256"], scope_key=record["scope_key"],
            title=snapshot["title"], text=snapshot["summary"] or snapshot["body"],
            reason=reason, verification=snapshot["verification"], lifecycle=snapshot["lifecycle"],
            freshness=freshness, authority="policy" if authority else "none",
            sources=tuple(snapshot["sources"]),
        )

    async def prepare(self, **kwargs):
        return await self._transaction(lambda conn: self.prepare_on(conn=conn, **kwargs))

    async def prepare_on(self, *, principal, required, query="", project_ids=(),
                         global_scope=False, pins=(), claim_epoch=None, budget=None,
                         tools=(), refresh=False, now=None, task_id=None, conn):
        bootstrap = isinstance(principal, BootstrapPrincipal)
        principal = principal.principal if bootstrap else principal
        if not principal or principal.unresolved:
            raise RecordError("record.forbidden")
        if (not isinstance(query, str) or len(query.encode()) > 4096
                or not isinstance(pins, (list, tuple)) or len(pins) > 100
                or type(refresh) is not bool or not isinstance(required, str)):
            raise RecordError("record.invalid_input")
        await conn.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"),
                           {"key": KNOWLEDGE_ERASURE_LOCK})
        owner = await self._owner(conn, principal, claim_epoch, bootstrap=bootstrap)
        if task_id is not None and owner["task_id"] != task_id:
            raise RecordError("record.stale_claim")
        principal = replace(principal, task_id=owner["task_id"])
        accesses = await self._scopes(conn, principal, project_ids, global_scope, bootstrap=bootstrap)
        budget = budget or ContextBudget.from_config(self.app_config)
        # A caller may reduce the budget, never bypass configured/operator limits.
        configured = ContextBudget.from_config(self.app_config)
        budget = replace(budget, **{
            k: min(getattr(budget, k), getattr(configured, k))
            for k in ("input_tokens", "knowledge_tokens", "knowledge_bytes",
                      "discovery_tokens", "discovery_items")
        }, output_tokens=max(budget.output_tokens, configured.output_tokens),
           wrapper_tokens=max(budget.wrapper_tokens, configured.wrapper_tokens))
        now = now or datetime.now(UTC)
        candidates, omissions, seen = [], [], set()
        for pin in pins:
            if not isinstance(pin, dict) or set(pin) != {"identity", "revision_id"}:
                raise RecordError("record.invalid_input", "Attachments require exact revisions")
            uuid_value(pin["revision_id"], "revision_id")
            found = None
            for access in accesses:
                try:
                    found = await self._item(conn, access, pin["identity"], pin["revision_id"],
                                             "pinned", now)
                    break
                except RecordError as exc:
                    if exc.code != "record.not_found":
                        raise
            if found is None:
                # Never retain denied identity/title/count in omission metadata.
                raise RecordError("record.not_found")
            key = (found.record_id, found.revision_id)
            if key not in seen:
                candidates.append(found)
                seen.add(key)
        if query and budget.discovery_items:
            for access in accesses:
                if not principal.policy.allows_aq_command("knowledge_search"):
                    raise RecordError("record.forbidden")
                rows = await lexical_search_on(conn, access=access, query=query,
                                                limit=budget.discovery_items)
                for row in rows["items"]:
                    item = await self._item(conn, access, f"record:{row['record_id']}",
                                            row["revision_id"], "lexical", now)
                    key = (item.record_id, item.revision_id)
                    if key not in seen:
                        candidates.append(item)
                        seen.add(key)
        selected, discovery_tokens, discovery_count = [], 0, 0
        required_account = budget.account(required, tools=tools)
        for item in candidates:
            next_discovery_tokens = len(context_markdown(
                [i for i in selected if i.reason == "lexical"] + [item],
            ).encode()) if item.reason == "lexical" else discovery_tokens
            reason = None
            if required_account["diagnostic"]:
                reason = "context.required_over_budget"
            elif item.reason == "lexical" and (
                item.freshness != "current" or item.lifecycle != "active"
                or item.verification == "disputed"
            ):
                reason = "ineligible"
            elif item.reason == "lexical" and (
                discovery_count >= budget.discovery_items
                or next_discovery_tokens > budget.discovery_tokens
            ):
                reason = "discovery_budget"
            elif not budget.account(required, context_markdown([*selected, item]), tools=tools)["fits"]:
                reason = "knowledge_budget"
            if reason:
                omissions.append(dict(record_id=item.record_id, revision_id=item.revision_id,
                                      reason=reason))
                continue
            selected.append(item)
            if item.reason == "lexical":
                discovery_count += 1
                discovery_tokens = next_discovery_tokens
        accounting = budget.account(required, context_markdown(selected), tools=tools)
        accounting.update(discovery_tokens=discovery_tokens, discovery_items=discovery_count)
        payload = [asdict(item) for item in selected]
        access_epoch = content_hash(dict(items=payload, scopes=[a.scope_key for a in accesses]))
        fingerprint = principal_fingerprint(principal)
        request = content_hash(dict(
            owner=owner, principal=fingerprint, access=access_epoch, query=query, pins=list(pins),
            required=sha256(required.encode()).hexdigest(), tools=tools,
            budget=accounting, omissions=omissions,
        ))
        if not refresh:
            cached = await conn.scalar(select(knowledge_context_bundles.c.selection).where(
                knowledge_context_bundles.c.request_fingerprint == request,
                knowledge_context_bundles.c.expires_at > now,
                knowledge_context_bundles.c.redacted_at.is_(None),
            ).order_by(knowledge_context_bundles.c.prepared_at.desc()).limit(1))
            if cached:
                return ContextBundle.from_dict(cached)
        bundle = ContextBundle(
            bundle_id=str(uuid4()), format_version=1, owner=owner,
            principal_fingerprint=fingerprint, access_epoch=access_epoch,
            scope_keys=tuple(a.scope_key for a in accesses), prepared_at=utc_text(now),
            expires_at=utc_text(now + timedelta(seconds=self.config.context.bundle_ttl_seconds)),
            budget=accounting, items=tuple(selected), omissions=tuple(omissions),
            content_sha256=sha256(context_markdown(selected).encode()).hexdigest(),
            request_fingerprint=request,
        )
        await conn.execute(insert(knowledge_context_bundles).values(
            bundle_id=UUID(bundle.bundle_id), owner_kind=owner["owner_kind"],
            owner_id=owner["owner_id"], session_instance=owner["session_instance"],
            claim_epoch=owner["claim_epoch"], principal_fingerprint=fingerprint,
            request_fingerprint=request, scope_keys=list(bundle.scope_keys), budget=accounting,
            selection=bundle.to_dict(), content_sha256=bundle.content_sha256,
            prepared_at=now, expires_at=datetime.fromisoformat(bundle.expires_at),
        ))
        return bundle

    async def _validate_bundle(self, conn, bundle, principal, claim_epoch):
        owner = await self._owner(conn, principal, claim_epoch)
        principal = replace(principal, task_id=owner["task_id"])
        if owner != bundle.owner or principal_fingerprint(principal) != bundle.principal_fingerprint:
            raise RecordError("context.execution_changed")
        if datetime.fromisoformat(bundle.expires_at) <= datetime.now(UTC):
            raise RecordError("context.expired")
        if not bundle.budget["fits"]:
            raise RecordError(bundle.budget["diagnostic"] or "context.over_budget")
        configured = ContextBudget.from_config(self.app_config)
        requested = ContextBudget(**bundle.budget["requested"])
        if any(getattr(requested, k) > getattr(configured, k) for k in (
            "input_tokens", "knowledge_tokens", "knowledge_bytes", "discovery_tokens",
            "discovery_items",
        )) or (requested.output_tokens < configured.output_tokens
               or requested.wrapper_tokens < configured.wrapper_tokens):
            raise RecordError("context.invalidated")
        scopes = [s.removeprefix("project:") for s in bundle.scope_keys if s != "global"]
        accesses = await self._scopes(conn, principal, scopes, "global" in bundle.scope_keys)
        for item in bundle.items:
            available = None
            for access in accesses:
                try:
                    available = await self._item(conn, access, f"record:{item.record_id}",
                                                 item.revision_id, item.reason, datetime.now(UTC))
                    if item.reason == "lexical":
                        record = await self.resolve_on(f"record:{item.record_id}", access, conn=conn)
                        current = await self._revision(record, conn=conn)
                        if str(current["revision_id"]) != item.revision_id:
                            raise RecordError("context.invalidated")
                    break
                except RecordError as exc:
                    if exc.code != "record.not_found":
                        raise
            if available != item:
                raise RecordError("context.invalidated")

    async def observe_delivery(self, **kwargs):
        return await self._transaction(lambda conn: self.observe_delivery_on(conn=conn, **kwargs))

    async def observe_delivery_on(self, *, bundle_id, principal, transport, transport_key,
                                  rendered_sha256, state="delivered", claim_epoch=None, conn):
        if not principal or not principal.policy.allows_aq_command("knowledge_context_deliver"):
            raise RecordError("record.forbidden")
        if state not in {"prepared", "delivered", "failed", "unknown"}:
            raise RecordError("record.invalid_input")
        for value in (transport, transport_key):
            if not isinstance(value, str) or not 1 <= len(value) <= 128:
                raise RecordError("record.invalid_input")
        await conn.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"),
                           {"key": KNOWLEDGE_ERASURE_LOCK})
        row = (await conn.execute(select(knowledge_context_bundles).where(
            knowledge_context_bundles.c.bundle_id == uuid_value(bundle_id, "bundle_id"),
            knowledge_context_bundles.c.redacted_at.is_(None),
        ).with_for_update())).mappings().first()
        if not row:
            raise RecordError("record.not_found")
        bundle = ContextBundle.from_dict(row["selection"])
        if (sha256(bundle.to_markdown().encode()).hexdigest() != bundle.content_sha256
                or bundle.content_sha256 != row["content_sha256"]):
            raise RecordError("context.digest_mismatch")
        await self._validate_bundle(conn, bundle, principal, claim_epoch)
        if rendered_sha256 != bundle.content_sha256:
            raise RecordError("context.digest_mismatch")
        values = dict(delivery_id=uuid4(), bundle_id=row["bundle_id"], transport=transport,
                      transport_key=transport_key, state=state, observed_at=datetime.now(UTC),
                      rendered_sha256=rendered_sha256)
        await conn.execute(pg_insert(knowledge_context_deliveries).values(**values)
                           .on_conflict_do_nothing(index_elements=["transport_key"]))
        existing = (await conn.execute(select(knowledge_context_deliveries).where(
            knowledge_context_deliveries.c.transport_key == transport_key,
        ).with_for_update())).mappings().one()
        if (existing["bundle_id"] != row["bundle_id"] or existing["transport"] != transport
                or existing["rendered_sha256"] != rendered_sha256):
            raise RecordError("record.idempotency_conflict")
        if existing["state"] != "delivered":
            await conn.execute(update(knowledge_context_deliveries).where(
                knowledge_context_deliveries.c.delivery_id == existing["delivery_id"],
            ).values(state=state, observed_at=values["observed_at"]))
        if state == "delivered":
            for item in bundle.items:
                await self._citation(conn, principal, bundle.owner, item, "injected",
                                     content_hash(dict(bundle=bundle_id, revision=item.revision_id)),
                                     bundle_id=row["bundle_id"])
        return dict(success=True, bundle_id=bundle_id,
                    delivery_id=str(existing["delivery_id"]),
                    state="delivered" if existing["state"] == "delivered" else state)

    async def _citation(self, conn, principal, owner, item, kind, key, *, bundle_id=None):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise RecordError("record.invalid_input")
        values = {k: owner[k] for k in (
            "owner_kind", "owner_id", "session_instance", "task_id", "attempt_id",
            "supervisor_session_id", "claim_epoch",
        )}
        await conn.execute(pg_insert(knowledge_citations).values(
            **values, citation_id=uuid4(), record_id=UUID(item.record_id),
            revision_id=UUID(item.revision_id), kind=kind, bundle_id=bundle_id,
            actor_id=f"session:{principal.session_id}", idempotency_key=key,
        ).on_conflict_do_nothing(constraint="uq_knowledge_citations_owner_key"))
        cited = (await conn.execute(select(knowledge_citations).where(
            knowledge_citations.c.owner_kind == owner["owner_kind"],
            knowledge_citations.c.owner_id == owner["owner_id"],
            knowledge_citations.c.session_instance == owner["session_instance"],
            knowledge_citations.c.kind == kind, knowledge_citations.c.idempotency_key == key,
        ))).mappings().one()
        if (str(cited["record_id"]) != item.record_id
                or str(cited["revision_id"]) != item.revision_id or cited["bundle_id"] != bundle_id):
            raise RecordError("record.idempotency_conflict")
        return str(cited["citation_id"])

    async def cite(self, *, principal, project_id, identity, revision_id, kind,
                   idempotency_key, claim_epoch=None):
        if not principal or not principal.policy.allows_aq_command("knowledge_cite"):
            raise RecordError("record.forbidden")
        if kind not in {"attached", "explicit_read"}:
            raise RecordError("record.invalid_input")
        uuid_value(revision_id, "revision_id")

        async def transaction(conn):
            await conn.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"),
                               {"key": KNOWLEDGE_ERASURE_LOCK})
            owner = await self._owner(conn, principal, claim_epoch)
            bound = replace(principal, task_id=owner["task_id"])
            access = await self._access(conn, bound, "knowledge_show", project_id)
            item = await self._item(conn, access, identity, revision_id, "pinned", datetime.now(UTC))
            return await self._citation(conn, principal, owner, item, kind, idempotency_key)

        return await self._transaction(transaction)
