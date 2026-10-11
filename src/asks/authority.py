"""Audience and verified-input authority, independent of model interpretation."""

from dataclasses import asdict

from sqlalchemy import select

from src.asks.models import AskError
from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.database.tables import messages, sessions
from src.sessions.spec import named_session_name


def visible(ask, principal=None, *, actor_id=None):
    principal = principal or current_principal() or TRUSTED_LOCAL
    if principal.project_id and principal.project_id != ask["project_id"]:
        return False
    if principal.kind is PrincipalKind.SESSION and not principal.elevated:
        return False
    if principal.kind not in {PrincipalKind.LOCAL, PrincipalKind.SESSION} and not actor_id:
        return False
    audience = ask["audience"]
    identity = actor_id or (
        "human:local-operator"
        if principal.kind is PrincipalKind.LOCAL
        else f"session:{principal.session_id}"
    )
    allowed = audience.get("principals")
    return not allowed or identity in allowed


def public_allowed(ask, config, *, actor_id=None):
    audience = ask["audience"]
    if audience.get("private") or audience.get("kind") == "private" or audience.get("principals"):
        return False
    settings = config.discord
    if actor_id and actor_id.removeprefix("human:discord:") not in {
        str(value) for value in settings.authorized_users
    }:
        return False
    channels = audience.get("channel_ids")
    return (not channels or str(settings.channel_id) in channels) and (
        not settings.project_id or settings.project_id == ask["project_id"]
    )


async def live_supervisor(project_id, *, conn, principal=None):
    principal = principal or current_principal() or TRUSTED_LOCAL
    if principal.kind is not PrincipalKind.SESSION or not principal.elevated:
        raise AskError("ask.not_supervisor", "A live named supervisor is required")
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
        row
        and row["profile_id"] == "supervisor"
        and row["lifecycle"] == "named"
        and row["state"] in {"starting", "running"}
        and row["desired_state"] == "running"
        and row["project_id"] == principal.project_id
        and row["project_id"] in (None, project_id)
        and row["name"] == named_session_name("supervisor", principal.project_id or "global")
        and principal.session_instance_token
        and row["instance_token"] == principal.session_instance_token
    ):
        raise AskError("ask.not_supervisor", "Supervisor scope or launch is no longer current")
    return row


async def addressed_supervisor(item, *, conn):
    row = await live_supervisor(item["project_id"], conn=conn)
    notice = (
        (
            await conn.execute(
                select(messages).where(
                    messages.c.id == item["supervisor_message_id"],
                )
            )
        )
        .mappings()
        .first()
    )
    if not (
        notice
        and notice["project_id"] == item["project_id"]
        and notice["to_id"] == f"supervisor-{row['project_id'] or 'global'}"
    ):
        raise AskError("ask.not_supervisor", "This input belongs to another supervisor")
    return row


async def authorize_input_actor(db, config, source, actor, *, conn, application=False):
    """An interpreter cannot mint, replace, or broaden the stored human evidence."""
    item = await db.get_ask_input(actor.input_id, conn=conn)
    ask = await db.get_ask(item["ask_id"], conn=conn) if item and item["ask_id"] else None
    if not (
        item
        and ask
        and actor.principal_id == item["verified_actor"]
        and item["state"] != "revoked"
        and (item["state"] != "expired" or (application and item["retention_entry_id"]))
        and item["routing_state"] != "quarantined"
        and ask["project_id"] == source.project_id
        and ask["source_kind"] == source.kind
        and ask["source_key"] == source.key
        and actor.provenance.get("source") == asdict(source)
        and ask["logical_owner"] == actor.provenance.get("owner")
        and visible(ask, actor_id=actor.principal_id)
    ):
        raise AskError("ask.not_decider", "Stored human input is not authorized for this source")
    if item["transport"] == "discord":
        if config is None or not public_allowed(ask, config, actor_id=actor.principal_id):
            raise AskError("ask.not_decider", "Input audience or allowlist was revoked")
    elif item["transport"] != "local" or item["verified_actor"] != "human:local-operator":
        raise AskError("ask.not_decider", "Input has no supported authenticated human authority")
    principal = current_principal() or TRUSTED_LOCAL
    if not (application and principal.kind in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}):
        await addressed_supervisor(item, conn=conn)
    return item
