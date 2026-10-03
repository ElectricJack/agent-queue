"""Bounded lexical metadata retrieval. No plugins, embeddings or task scans."""

import base64
import json
import math
from uuid import UUID

from sqlalchemy import Float, case, cast, func, literal, or_, select, tuple_

from src.database.tables import (
    knowledge_records,
    knowledge_revision_payloads,
    knowledge_search,
    records,
)
from src.knowledge.models import content_hash
from src.records.models import RecordError


async def lexical_search_on(
    conn,
    *,
    access,
    query="",
    category=None,
    include_retired=False,
    include_disputed=False,
    limit=25,
    cursor=None,
):
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or not isinstance(query, str)
        or len(query.encode()) > 4096
        or type(include_retired) is not bool
        or type(include_disputed) is not bool
        or (
            category is not None
            and category
            not in {"fact", "decision", "policy", "procedure", "incident", "reference", "note"}
        )
    ):
        raise RecordError("record.invalid_input", "Invalid search bounds or filters")
    scope = f"project:{access.project_id}"
    principal = access.principal
    binding = content_hash(
        dict(
            scope=scope,
            actor=access.actor_key,
            query=query,
            category=category,
            retired=include_retired,
            disputed=include_disputed,
            limit=limit,
            policy=sorted(principal.policy.aq_commands),
            elevated=principal.elevated,
        )
    )
    s = knowledge_search.c
    tsquery = func.websearch_to_tsquery("simple", query)
    exact = case((or_(records.c.knowledge_alias == query.lower(), s.title == query), 1), else_=0)
    verified = case((s.verification == "verified", 1), else_=0)
    rank = cast(func.ts_rank(s.search_vector, tsquery), Float) if query else literal(0.0)
    stmt = (
        select(
            s.record_id,
            records.c.knowledge_alias,
            s.revision_id,
            s.title,
            s.summary,
            s.category,
            s.lifecycle,
            s.verification,
            s.updated_at,
            exact.label("exact"),
            verified.label("verified"),
            rank.label("rank"),
        )
        .join(records, records.c.record_id == s.record_id)
        .join(
            knowledge_records,
            (knowledge_records.c.record_id == s.record_id)
            & (knowledge_records.c.current_revision_id == s.revision_id),
        )
        .join(
            knowledge_revision_payloads, knowledge_revision_payloads.c.revision_id == s.revision_id
        )
        .where(
            records.c.scope_key == scope,
            s.scope_key == scope,
            knowledge_revision_payloads.c.snapshot.is_not(None),
        )
    )
    if query:
        stmt = stmt.where(or_(s.search_vector.op("@@")(tsquery), exact == 1))
    if category:
        stmt = stmt.where(s.category == category)
    if not include_retired:
        stmt = stmt.where(s.lifecycle == "active")
    if not include_disputed:
        stmt = stmt.where(s.verification != "disputed")
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise ValueError()
            page = json.loads(base64.urlsafe_b64decode(cursor))
            position = page["position"]
            if (
                page["binding"] != binding
                or len(position) != 4
                or any(type(v) is not int or v not in (0, 1) for v in position[:2])
                or not isinstance(position[2], (int, float))
                or not math.isfinite(position[2])
            ):
                raise ValueError()
            stmt = stmt.where(
                tuple_(exact, verified, rank, s.record_id)
                < tuple_(
                    position[0],
                    position[1],
                    position[2],
                    UUID(position[3]),
                )
            )
        except (ValueError, TypeError, KeyError, IndexError):
            raise RecordError("record.invalid_cursor") from None
    rows = (
        (
            await conn.execute(
                stmt.order_by(exact.desc(), verified.desc(), rank.desc(), s.record_id.desc()).limit(
                    limit + 1
                )
            )
        )
        .mappings()
        .all()
    )
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps(
                dict(
                    binding=binding,
                    position=[
                        last["exact"],
                        last["verified"],
                        last["rank"],
                        str(last["record_id"]),
                    ],
                )
            ).encode()
        ).decode()
    items = []
    for row in rows[:limit]:
        item = {
            key: row[key]
            for key in (
                "knowledge_alias",
                "title",
                "summary",
                "category",
                "lifecycle",
                "verification",
            )
        }
        item.update(
            kind="knowledge",
            record_id=str(row["record_id"]),
            revision_id=str(row["revision_id"]),
            updated_at=row["updated_at"].isoformat(),
        )
        items.append(item)
    return dict(success=True, outcome="read", items=items, next_cursor=next_cursor)
