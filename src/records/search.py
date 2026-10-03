"""Mixed metadata pages: lexical knowledge followed by stable task IDs.

This projection reads task domains directly, including unmapped archived tasks.
Only returned authorized tasks acquire record mappings. It never changes execution.
Knowledge facets apply only to knowledge; they are not task status filters.
"""

import base64
import binascii
import json

from sqlalchemy import literal, or_, select, union_all

from src.database.tables import archived_tasks, tasks
from src.knowledge.models import content_hash
from src.knowledge.search import lexical_search_on, validate_search_bounds
from src.records.auth import GLOBAL_SCOPE
from src.records.models import RecordError


async def search_on(
    service,
    *,
    conn,
    principal,
    project_id,
    kind="knowledge",
    query="",
    category=None,
    lifecycle=None,
    verification=None,
    include_retired=False,
    include_disputed=False,
    limit=25,
    cursor=None,
):
    if kind not in {"task", "knowledge", "all"}:
        raise RecordError("record.invalid_input", "Choose task, knowledge or all")
    filters = dict(
        query=query,
        category=category,
        lifecycle=lifecycle,
        verification=verification,
        include_retired=include_retired,
        include_disputed=include_disputed,
        limit=limit,
    )
    validate_search_bounds(**filters)
    access = await service._access(conn, principal, "record_search", project_id)
    binding = content_hash(
        dict(
            scope=access.scope_key,
            actor=access.actor_key,
            kind=kind,
            filters=filters,
            grants=sorted(principal.policy.aq_commands),
            elevated=access.supervisor,
        )
    )
    stage, position = ("task" if kind == "task" else "knowledge"), None
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 8192:
                raise ValueError()
            page = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            stage, position = page["stage"], page["position"]
            if (
                page["binding"] != binding
                or stage not in {"knowledge", "task"}
                or kind not in {stage, "all"}
                or (position is not None and not isinstance(position, str))
            ):
                raise ValueError()
        except (ValueError, TypeError, KeyError, binascii.Error):
            raise RecordError("record.invalid_cursor") from None

    def token(next_stage, next_position):
        return base64.urlsafe_b64encode(
            json.dumps(dict(binding=binding, stage=next_stage, position=next_position)).encode()
        ).decode()

    items = []
    if stage == "knowledge":
        page = await lexical_search_on(conn, access=access, cursor=position, **filters)
        from src.knowledge.authority import authority_on

        for item in page["items"]:
            item["authoritative"] = False
            if item["verification"] == "verified":
                record = await service.resolve_on(f"record:{item['record_id']}", access, conn=conn)
                revision = await service._revision(record, item["revision_id"], conn=conn)
                item["authoritative"] = bool(
                    await authority_on(
                        conn,
                        record,
                        revision,
                        review_required=service.config.authority_review_required,
                    )
                )
        items.extend(page["items"])
        if page["next_cursor"]:
            return dict(
                success=True,
                outcome="read",
                items=items,
                next_cursor=token("knowledge", page["next_cursor"]),
            )
        if kind == "knowledge":
            return dict(success=True, outcome="read", items=items, next_cursor=None)
        position = None
    if access.project_id is GLOBAL_SCOPE:
        return dict(success=True, outcome="read", items=items, next_cursor=None)
    projections = []
    for table, archived in ((tasks, False), (archived_tasks, True)):
        stmt = select(
            table.c.id,
            table.c.title,
            table.c.status,
            table.c.updated_at,
            literal(archived).label("archived"),
        ).where(table.c.project_id == access.project_id)
        if not access.supervisor:
            stmt = stmt.where(table.c.id == principal.task_id)
        if query:
            # User punctuation is literal, not an ILIKE pattern.
            stmt = stmt.where(
                or_(table.c.id == query, table.c.title.icontains(query, autoescape=True))
            )
        if position is not None:
            stmt = stmt.where(table.c.id < position)
        projections.append(stmt)
    remaining = limit - len(items)
    domain = union_all(*projections).subquery()
    rows = (
        (await conn.execute(select(domain).order_by(domain.c.id.desc()).limit(remaining + 1)))
        .mappings()
        .all()
    )
    for row in rows[:remaining]:
        record = await service.resolve_on(f"task:{row['id']}", access, conn=conn)
        items.append(
            dict(
                kind="task",
                record_id=str(record["record_id"]),
                task_id=row["id"],
                title=row["title"],
                status=row["status"],
                archived=row["archived"],
                updated_at=row["updated_at"],
            )
        )
    next_cursor = (
        token("task", rows[remaining - 1]["id"] if remaining else None)
        if len(rows) > remaining
        else None
    )
    return dict(success=True, outcome="read", items=items, next_cursor=next_cursor)
