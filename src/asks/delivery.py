"""One canonical ask root, with durable fencing across unknown HTTP outcomes.

The scheduler calls this adapter inside a CommandHandler-owned service boundary.
External I/O never holds source/ask transactions. Each attempt records its fence
before I/O; lease recovery may only reconcile that exact attempt, never resend it.
"""

import logging
import secrets
import time

from sqlalchemy import and_, select, update

from src.asks.authority import public_allowed
from src.asks.models import AskError, SourceIdentity, digest
from src.asks.render import render
from src.asks.service import new_id
from src.database.tables import (
    ask_action_tokens as tokens,
    human_asks,
    human_communication_modes as modes,
    outbound_deliveries as outbox,
    projects,
)
from src.escalations.interactions import ButtonSpec
from src.escalations.plan import MAX_ATTEMPTS, backoff_for
from src.escalations.transport import (
    TransportMissing,
    TransportRejected,
    TransportRetryable,
    TransportUnavailable,
)

TOKEN_TTL = 7 * 86400
logger = logging.getLogger(__name__)


def destination(config):
    return f"discord:{config.discord.guild_id}:{config.discord.channel_id}"


def config_fingerprint(config):
    d = config.discord
    return digest(
        [
            str(d.guild_id),
            str(d.channel_id),
            sorted(str(u) for u in d.authorized_users),
            d.project_id,
        ]
    )


class AskDeliveryAdapter:
    def __init__(self, handler, transport, *, lease_owner, links=None, clock=time.time):
        self.handler, self.db, self.config = handler, handler.db, handler.config
        self.transport, self.lease_owner, self.links = transport, lease_owner, links
        self.clock = clock
        self._scan_after = None

    async def current(self, ask_id, *, conn):
        # Source first, then ask; no lock is taken by the initial identity read.
        ask = await self.db.get_ask(ask_id, conn=conn)
        if not ask:
            raise AskError("ask.not_found", "Request was not found")
        source = SourceIdentity(
            ask["project_id"], ask["source_kind"], ask["source_key"], ask["source_identity"]
        )
        return (await self.handler._ask_service().publish(source, legacy=True, conn=conn)).ask

    async def eligible(self, ask, *, conn, row=None):
        if (
            await conn.scalar(select(projects.c.status).where(projects.c.id == ask["project_id"]))
            == "PAUSED"
        ):
            return None, "project paused"
        mode = await self.db.get_ask_mode(
            ask["project_id"], destination(self.config), conn=conn, lock=True
        )
        if (
            not mode
            or mode["mode"] != "channel"
            or mode["config_fingerprint"] != config_fingerprint(self.config)
            or not self.config.discord.authorized_users
            or not public_allowed(ask, self.config)
        ):
            return None, "delivery ownership or audience changed"
        if row:
            if (
                row["mode_generation"] != mode["generation"]
                or row["decision_generation"] != ask["decision_generation"]
                or row["display_version"] != ask["display_version"]
            ):
                return None, "source or render changed"
            if row["send_kind"] == "reply":
                item = await self.db.get_ask_input(row["payload"].get("input_id"), conn=conn)
                if (
                    not item
                    or item["state"] in {"expired", "revoked"}
                    or item["routing_state"] == "quarantined"
                    or not public_allowed(ask, self.config, actor_id=item["verified_actor"])
                ):
                    return None, "reply authority revoked"
        return mode, None

    async def reconcile(self, *, limit=100):
        # Configuration expresses desired delivery, never permission to revive a
        # retired writer. Persist its fence so reverting YAML cannot re-enable it.
        async with self.db.immediate() as conn:
            await conn.execute(
                update(modes)
                .where(
                    modes.c.config_fingerprint != config_fingerprint(self.config),
                )
                .values(
                    mode="delivery_disabled",
                    generation=modes.c.generation + 1,
                    migrated=True,
                    config_fingerprint=config_fingerprint(self.config),
                    updated_at=self.clock(),
                )
            )
        # Keyset rotation also advances past private or unavailable sources that
        # have no binding yet. Such rows must not monopolize the first batch.
        stmt = (
            select(human_asks.c.id)
            .join(
                modes,
                and_(
                    modes.c.project_id == human_asks.c.project_id,
                    modes.c.destination == destination(self.config),
                ),
            )
            .join(projects, projects.c.id == human_asks.c.project_id)
            .where(modes.c.mode == "channel", projects.c.status != "PAUSED")
            .order_by(human_asks.c.id)
            .limit(limit)
        )
        async with self.db._engine.connect() as conn:
            page = stmt.where(human_asks.c.id > self._scan_after) if self._scan_after else stmt
            ids = list((await conn.execute(page)).scalars())
            if not ids and self._scan_after:
                ids = list((await conn.execute(stmt)).scalars())
        self._scan_after = ids[-1] if ids else None
        for ask_id in ids:
            try:
                await self.enqueue(ask_id)
            except Exception:
                logger.warning("Ask source unavailable for delivery: %s", ask_id, exc_info=True)
        async with self.db.immediate() as conn:
            unknown = await self.db.unresolved_ask_deliveries(conn=conn, limit=limit)
        for row in unknown:
            if row["state"] == "unknown" or (row["lease_expires_at"] or 0) < self.clock():
                await self.recover_safely(row)
        # Explicitly abandoned writes can still finish late. Preserve their receipt,
        # then disable that duplicate root through the same bounded outbox.
        async with self.db._engine.connect() as conn:
            abandoned = (
                (
                    await conn.execute(
                        select(outbox)
                        .where(
                            outbox.c.owner_kind == "ask",
                            outbox.c.state == "cancelled",
                            outbox.c.write_started_at.is_not(None),
                            outbox.c.send_kind.in_(["root", "root_edit"]),
                            outbox.c.external_receipt_id.is_(None),
                        )
                        .order_by(outbox.c.updated_at)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
        for row in abandoned:
            await self.recover_safely(dict(row))

    async def recover_safely(self, row):
        try:
            await self.recover(row)
        except Exception:
            logger.warning("Ask delivery reconciliation failed: %s", row["id"], exc_info=True)
        finally:
            # Rotate failures and abandoned attempts too; they retain their fence.
            async with self.db.immediate() as conn:
                await conn.execute(
                    update(outbox).where(outbox.c.id == row["id"]).values(updated_at=self.clock())
                )

    async def enqueue(self, ask_id):
        link = await self.links.resolve() if self.links else None
        async with self.db.immediate() as conn:
            ask = await self.current(ask_id, conn=conn)
            mode, reason = await self.eligible(ask, conn=conn)
            if reason:
                return None
            now = self.clock()
            binding = await self.db.active_ask_binding(ask_id, mode["destination"], conn=conn)
            if not binding:
                if ask["decision_state"] != "pending":
                    return None
                binding = await self.db.insert_ask_binding(
                    {
                        "id": new_id("abind"),
                        "ask_id": ask_id,
                        "project_id": ask["project_id"],
                        "destination": mode["destination"],
                        "guild_id": str(self.config.discord.guild_id),
                        "channel_id": str(self.config.discord.channel_id),
                        "mode_generation": mode["generation"],
                        "created_at": now,
                        "updated_at": now,
                    },
                    conn=conn,
                )
            if binding["mode_generation"] != mode["generation"]:
                if binding["inflight_id"]:
                    return None
                await self.db.retire_ask_tokens(binding["id"], now=now, conn=conn)
                binding = await self.db.update_ask_binding(
                    binding["id"],
                    {"mode_generation": mode["generation"], "applied_display_version": 0},
                    conn=conn,
                )
            # Rendering refreshes expiring opaque controls without changing decision meaning.
            expiry = await conn.scalar(
                select(tokens.c.expires_at)
                .where(
                    tokens.c.binding_id == binding["id"],
                    tokens.c.revoked_at.is_(None),
                )
                .order_by(tokens.c.expires_at)
                .limit(1)
            )
            if expiry and expiry <= now + 3600 and not binding["inflight_id"]:
                ask = await self.db.update_ask_projection(
                    ask_id, {"display_version": ask["display_version"] + 1}, conn=conn
                )
            binding = await self.db.update_ask_binding(
                binding["id"],
                {"desired_display_version": ask["display_version"], "updated_at": now},
                conn=conn,
            )
            for alternate in binding["alternate_roots"]:
                if alternate.get("confirmed") and alternate.get("id"):
                    await self.reserve_redirect(ask, binding, alternate, conn=conn)
            if binding["inflight_id"] or binding["root_state"] in {"unknown", "missing"}:
                return None
            if binding["applied_display_version"] >= ask["display_version"]:
                return None
            card = render(
                ask,
                base_url=link.url if link else "",
                notice=link.unavailable_notice if link else "",
            )
            if not link or not link.url:
                await self.repair_case(
                    ask, binding, "Remote dashboard origin is not configured", conn=conn
                )
                return None
            await self.db.resolve_ask_delivery_case(
                binding["id"],
                "Remote dashboard origin is not configured",
                {"configured_origin": link.url},
                conn=conn,
                now=now,
            )
            kind = "root_edit" if binding["external_root_id"] else "root"
            key = self.operation_key(ask, binding, kind)
            existing = await conn.scalar(select(outbox.c.id).where(outbox.c.dedup_key == key))
            if existing:
                return existing
            buttons = []
            if kind == "root_edit":
                await self.db.retire_ask_tokens(binding["id"], now=now, conn=conn)
                for choice in card.choices:
                    token = secrets.token_urlsafe(24)
                    await conn.execute(
                        tokens.insert().values(
                            token=token,
                            ask_id=ask_id,
                            binding_id=binding["id"],
                            decision_generation=ask["decision_generation"],
                            source_fingerprint=ask["source_fingerprint"],
                            feedback_version=ask["feedback_version"],
                            display_version=ask["display_version"],
                            mode_generation=binding["mode_generation"],
                            root_generation=binding["root_generation"],
                            root_message_id=binding["external_root_id"],
                            option_key=choice["key"],
                            audience=ask["audience"],
                            created_at=now,
                            expires_at=now + TOKEN_TTL,
                        )
                    )
                    buttons.append(
                        {
                            "custom_id": f"aqask:{token}",
                            "label": choice["label"],
                            "kind": "choice",
                            "escalation_id": ask_id,
                        }
                    )
            row = await self.reserve(
                ask,
                binding,
                kind,
                {
                    "text": card.text,
                    "buttons": buttons,
                    "source_fingerprint": ask["source_fingerprint"],
                },
                key=key,
                conn=conn,
            )
            return row["id"]

    @staticmethod
    def operation_key(ask, binding, kind, entry_id=""):
        return "ask-send:" + digest(
            [
                ask["id"],
                ask["decision_generation"],
                ask["display_version"],
                binding["destination"],
                binding["mode_generation"],
                binding["root_generation"],
                kind,
                entry_id,
            ]
        )

    async def reserve(self, ask, binding, kind, payload, *, key, conn):
        row, _ = await self.db.reserve_outbound_delivery_in_transaction(
            conn,
            owner_kind="ask",
            owner_id=ask["id"],
            dedup_key=key,
            destination={"transport": "discord", "channel_id": binding["channel_id"]},
            payload={**payload, "ask_id": ask["id"]},
            due_at=self.clock(),
            now=self.clock(),
            ask_fields={
                "ask_id": ask["id"],
                "binding_id": binding["id"],
                "project_id": ask["project_id"],
                "decision_generation": ask["decision_generation"],
                "display_version": ask["display_version"],
                "mode_generation": binding["mode_generation"],
                "root_generation": binding["root_generation"],
                "send_kind": kind,
            },
        )
        return row

    async def repair_case(self, ask, binding, reason, *, conn, row=None):
        return await self.db.insert_ask_reconciliation_case(
            {
                "id": new_id("arepair"),
                "project_id": ask["project_id"],
                "conflict_key": f"delivery:{binding['id']}:{(row or {}).get('id', reason)}",
                "source_evidence": {
                    "ask_id": ask["id"],
                    "binding_id": binding["id"],
                    "root_state": binding["root_state"],
                },
                "affected_deliveries": [row["id"]] if row else [],
                "logical_owner": ask["logical_owner"],
                "reason": f"Delivery unknown: {reason}",
                "created_at": self.clock(),
                "updated_at": self.clock(),
            },
            conn=conn,
        )

    async def reserve_reply(self, ask, item, text, *, entry_id, conn):
        mode = await self.db.get_ask_mode(ask["project_id"], destination(self.config), conn=conn)
        if not mode or mode["mode"] != "channel":
            return None
        binding = await self.db.active_ask_binding(ask["id"], mode["destination"], conn=conn)
        if not binding:
            binding = await self.db.insert_ask_binding(
                {
                    "id": new_id("abind"),
                    "ask_id": ask["id"],
                    "project_id": ask["project_id"],
                    "destination": mode["destination"],
                    "guild_id": str(self.config.discord.guild_id),
                    "channel_id": str(self.config.discord.channel_id),
                    "mode_generation": mode["generation"],
                    "created_at": self.clock(),
                    "updated_at": self.clock(),
                },
                conn=conn,
            )
        row = await self.reserve(
            ask,
            binding,
            "reply",
            {
                "text": text,
                "input_id": item["id"],
                "entry_id": entry_id,
                "reference_message_id": item["external_message_id"],
            },
            key=self.operation_key(ask, binding, "reply", entry_id),
            conn=conn,
        )
        return row["id"]

    async def adopt_reply(self, row):
        """Fence pre-cutover conversation rows before any new external write."""
        if row.get("reclaimed") or row["attempt_count"] > 1:
            await self.db.finish_outbound_delivery(
                row["id"],
                lease_owner=self.lease_owner,
                status="unknown",
                now=self.clock(),
                last_error="Legacy reply needs receipt reconciliation before adoption",
            )
            return
        async with self.db.immediate() as conn:
            ask = await self.current(row["payload"]["ask_id"], conn=conn)
            item = await self.db.get_ask_input(row["payload"].get("input_id"), conn=conn)
            if item:
                await self.reserve_reply(
                    ask, item, row["payload"]["text"], entry_id=row["dedup_key"], conn=conn
                )
            await self.db.defer_ask_delivery(
                row, now=self.clock(), conn=conn, reason="Adopted by ask delivery", cancel=True
            )

    async def reserve_redirect(self, ask, binding, alternate, *, conn):
        root = alternate["id"]
        target = binding["external_root_id"]
        if not target or root == target:
            return
        text = (
            "This request moved. Use the current card: "
            f"https://discord.com/channels/{binding['guild_id']}/{binding['channel_id']}/{target}"
        )
        await self.reserve(
            ask,
            binding,
            "redirect",
            {"text": text, "redirect_root_id": root},
            key=self.operation_key(ask, binding, "redirect", root),
            conn=conn,
        )

    async def deliver(self, row):
        if row.get("write_started_at") is not None:
            await self.recover(row)
            return
        async with self.db.immediate() as conn:
            ask = await self.current(row["ask_id"], conn=conn)
            mode, reason = await self.eligible(ask, conn=conn, row=row)
            binding = await self.db.get_ask_binding(row["binding_id"], conn=conn, lock=True)
            if not reason and (
                not binding["active"]
                or binding["root_generation"] != row["root_generation"]
                or binding["mode_generation"] != row["mode_generation"]
            ):
                reason = "root binding changed"
            if (
                not reason
                and row["payload"].get("source_fingerprint", ask["source_fingerprint"])
                != ask["source_fingerprint"]
            ):
                reason = "source changed"
            if reason:
                await self.db.defer_ask_delivery(
                    row,
                    now=self.clock(),
                    conn=conn,
                    reason=reason,
                    cancel=reason != "project paused",
                )
                if reason == "source or render changed" and row["send_kind"] == "reply":
                    item = await self.db.get_ask_input(row["payload"]["input_id"], conn=conn)
                    if item:
                        await self.reserve_reply(
                            ask,
                            item,
                            row["payload"]["text"],
                            entry_id=row["payload"]["entry_id"],
                            conn=conn,
                        )
                return
            started = await self.db.begin_ask_write(
                row, now=self.clock(), attempt_token=secrets.token_hex(16), conn=conn
            )
        if not started:
            return
        row = started
        content = row["payload"]["text"] + "\n" + row["marker"]
        buttons = tuple(ButtonSpec(**b) for b in row["payload"].get("buttons", []))
        try:
            if row["send_kind"] in {"root_edit", "redirect"}:
                root = row["payload"].get("redirect_root_id") or binding["external_root_id"]
                await self.transport.edit_root(
                    channel_id=binding["channel_id"],
                    root_message_id=root,
                    content=content,
                    buttons=buttons,
                )
                receipt = root
            else:
                outcome = await self.transport.post_root(
                    channel_id=binding["channel_id"],
                    content=content,
                    buttons=buttons,
                    reference_message_id=(
                        row["payload"].get("reference_message_id") or binding["external_root_id"]
                    )
                    if row["send_kind"] == "reply"
                    else None,
                )
                receipt = outcome.receipt_id
            if not receipt:
                await self.finish(row, status="unknown", error="Missing confirmed receipt")
            else:
                await self.finish(row, status="sent", receipt=receipt)
        except TransportMissing:
            if row["send_kind"] == "root_edit":
                await self.replace_deleted(row)
            else:
                await self.finish(row, status="cancelled", error="Target was confirmed deleted")
        except (TransportRetryable, TransportUnavailable) as exc:
            await self.finish(
                row,
                status="retry" if row["attempt_count"] < MAX_ATTEMPTS else "unknown",
                error=str(exc),
                retry_at=self.clock() + backoff_for(row["attempt_count"]),
            )
        except TransportRejected as exc:
            await self.finish(row, status="unknown", error=str(exc))
        except Exception:
            await self.recover(row)

    async def recover(self, row):
        # Read-only transport reconciliation is permitted while paused or during transfer.
        try:
            if row["send_kind"] in {"root_edit", "redirect"}:
                async with self.db._engine.connect() as conn:
                    binding = await self.db.get_ask_binding(row["binding_id"], conn=conn)
                root = row["payload"].get("redirect_root_id") or binding["external_root_id"]
                if row["state"] == "cancelled":
                    root = next(
                        (
                            a["id"]
                            for a in binding["alternate_roots"]
                            if a["operation_id"] == row["id"]
                        ),
                        None,
                    )
                    if not root:
                        return
                content = await self.transport.read_root(
                    channel_id=row["destination"]["channel_id"], root_message_id=root
                )
                from src.escalations.transport import SendOutcome

                found = SendOutcome(root) if row["marker"] in content else None
            else:
                found = await self.transport.find_marker(
                    channel_id=row["destination"]["channel_id"],
                    thread_id=None,
                    marker=row["marker"],
                )
        except TransportMissing:
            if row["send_kind"] == "root_edit" and row["state"] != "cancelled":
                await self.replace_deleted(row)
                return
            found = None
        except Exception:
            found = None
        receipt = found.receipt_id if found else None
        if found and found.channel_id not in (None, row["destination"]["channel_id"]):
            receipt = None
        if receipt:
            async with self.db._engine.connect() as conn:
                binding = await self.db.get_ask_binding(row["binding_id"], conn=conn)
            if (
                row["state"] != "cancelled"
                and row["send_kind"] == "root_edit"
                and receipt != binding["external_root_id"]
            ):
                receipt = None
        await self.finish(
            row,
            status="sent" if receipt else "unknown",
            receipt=receipt,
            error=None if receipt else "HTTP completion is unknown; nothing was reposted",
        )

    async def finish(self, row, **result):
        async with self.db.immediate() as conn:
            saved = await self.db.complete_ask_write(row, conn=conn, now=self.clock(), **result)
            if saved and result["status"] == "sent":
                await self.db.resolve_ask_delivery_case(
                    row["binding_id"],
                    row["id"],
                    {"receipt": result["receipt"]},
                    conn=conn,
                    now=self.clock(),
                )
            if saved and result["status"] == "unknown":
                ask = await self.db.get_ask(row["ask_id"], conn=conn)
                binding = await self.db.get_ask_binding(row["binding_id"], conn=conn)
                await self.repair_case(
                    ask, binding, result.get("error", "Unknown outcome"), row=row, conn=conn
                )
        if saved and result["status"] == "sent":
            # Re-read the source after completion; never reuse the in-flight snapshot.
            await self.enqueue(row["ask_id"])
        elif not saved and result["status"] == "sent":
            await self.late_receipt(row, result["receipt"])

    async def late_receipt(self, row, receipt):
        async with self.db.immediate() as conn:
            ask = await self.current(row["ask_id"], conn=conn)
            binding = await self.db.get_ask_binding(row["binding_id"], conn=conn, lock=True)
            alternates = [dict(a) for a in binding["alternate_roots"]]
            alternate = next((a for a in alternates if a["operation_id"] == row["id"]), None)
            if not alternate:
                return
            alternate.update(id=receipt, confirmed=True)
            await conn.execute(
                update(outbox)
                .where(outbox.c.id == row["id"])
                .values(
                    external_receipt_id=receipt,
                    receipt_confirmed_at=self.clock(),
                    updated_at=self.clock(),
                )
            )
            binding = await self.db.update_ask_binding(
                binding["id"], {"alternate_roots": alternates}, conn=conn
            )
        await self.enqueue(ask["id"])

    async def replace_deleted(self, row, *, reason="deleted", conn=None):
        if conn is not None:
            return await self._replace_on(row, reason=reason, conn=conn)
        async with self.db.immediate() as conn:
            await self.current(row["ask_id"], conn=conn)
            await self._replace_on(row, reason=reason, conn=conn)
        await self.enqueue(row["ask_id"])

    async def _replace_on(self, row, *, reason, conn):
        binding = await self.db.get_ask_binding(row["binding_id"], conn=conn, lock=True)
        if binding["root_generation"] != row["root_generation"] or binding["inflight_id"] not in (
            None,
            row["id"],
        ):
            return False
        await conn.execute(
            update(outbox)
            .where(outbox.c.id == row["id"])
            .values(
                state="cancelled",
                lease_owner=None,
                lease_expires_at=None,
                last_error=reason,
                updated_at=self.clock(),
            )
        )
        await self.db.retire_ask_tokens(binding["id"], now=self.clock(), conn=conn)
        await self.db.update_ask_binding(
            binding["id"],
            {
                "root_generation": binding["root_generation"] + 1,
                "external_root_id": None,
                "alternate_roots": [
                    *binding["alternate_roots"],
                    {
                        "id": binding["external_root_id"],
                        "generation": binding["root_generation"],
                        "reason": reason,
                        "operation_id": row["id"],
                        "receipt": binding["receipt"],
                    },
                ],
                "inflight_id": None,
                "root_state": "pending",
                "receipt": None,
                "applied_display_version": 0,
                "updated_at": self.clock(),
            },
            conn=conn,
        )
        return True
