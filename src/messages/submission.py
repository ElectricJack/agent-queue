"""Shared validation for ordinary sends and caller-transaction notifications.

No writes or delivery are performed here. Collaboration threads keep their own
membership/sequence transaction and are handled by the existing command branch.
"""
from __future__ import annotations

from dataclasses import asdict

from sqlalchemy import select

from src.collaboration import is_collaboration_thread
from src.commands.principal import PrincipalKind, current_principal
from src.models import MESSAGE_TO_KINDS

_SUPERVISOR_ADDRESS_PREFIX = "supervisor-"
_GLOBAL_SUPERVISOR_ADDRESS = "supervisor-global"


async def lookup(handler, table_name, identity, conn=None):
    if conn is None:
        getter = {"projects": "get_project", "tasks": "get_task", "sessions": "get_session",
                  "messages": "get_message"}[table_name]
        row = await getattr(handler.db, getter)(identity)
        return asdict(row) if row is not None else None
    from src.database import tables

    table = getattr(tables, table_name)
    return (await conn.execute(select(table).where(table.c.id == identity))).mappings().first()


async def recipient_project(handler, kind, identity, conn=None):
    table = {"task": "tasks", "session": "sessions"}.get(kind)
    if table:
        row = await lookup(handler, table, identity, conn)
        return row["project_id"] if row else None
    return None


async def supervisor_mailbox(handler, identity, project_id, conn=None):
    suffix = identity.removeprefix(_SUPERVISOR_ADDRESS_PREFIX)
    if identity == _GLOBAL_SUPERVISOR_ADDRESS or (
            suffix and await lookup(handler, "projects", suffix, conn) is not None):
        return identity
    return _SUPERVISOR_ADDRESS_PREFIX + project_id if project_id else None


def system_scope_error(handler):
    principal = current_principal()
    if principal and principal.kind is PrincipalKind.PLAYBOOK:
        if principal.project_id is None:
            return None
        return {"success": False, "error_code": "unauthorized", "error": "unauthorized"}
    return handler._system_message_scope_error()


async def authorize_submission(handler, args, kind, identity, project_id, conn):
    """Reauthorize playbook recipients, including suppressed/replayed notifications."""
    principal = current_principal()
    if principal is None or principal.kind is not PrincipalKind.PLAYBOOK:
        return None
    denied = {"success": False, "error": "unauthorized", "error_code": "unauthorized"}
    if not principal.policy.allows_aq_command("message_send"):
        return denied
    if principal.project_id is not None and project_id != principal.project_id:
        return denied
    owner = None
    if kind == "session" and identity.startswith(("supervisor-", "n-supervisor--")):
        suffix = identity.removeprefix("supervisor-") if identity.startswith("supervisor-") else (
            identity.removeprefix("n-supervisor--"))
        if suffix != "global":
            row = await lookup(handler, "projects", suffix, conn)
            if row is None:
                return denied
            owner = suffix
    elif kind in {"task", "session"}:
        row = await lookup(handler, "tasks" if kind == "task" else "sessions", identity, conn)
        if row is None:
            return denied
        owner = row["project_id"]
    elif kind == "profile" and identity == "supervisor":
        owner = project_id
    elif principal.project_id is not None:
        return denied
    if principal.project_id is not None and owner != principal.project_id:
        return denied
    for field in ("reply_to_id",):
        if args.get(field):
            row = await lookup(handler, "messages", args[field], conn)
            if row is None or (principal.project_id and row["project_id"] != principal.project_id):
                return denied
    if args.get("thread_id"):
        # Free-form threads remain labels; a thread cannot grant access to its messages.
        if is_collaboration_thread(args["thread_id"]):
            return denied
    return None


def playbook_sender(namespace=None):
    from src.playbooks.invocation import current_invocation

    invocation = current_invocation()
    if invocation is None:
        return None
    return dict(from_kind="system", from_id="playbook:" + invocation.artifact_ref.playbook_id,
                provenance=dict(run_id=invocation.run_id,
                                playbook_id=invocation.artifact_ref.playbook_id,
                                namespace=namespace.as_dict() if namespace else None,
                                step_id=invocation.step_id))


async def prepare_submission(handler, args, *, conn=None):
    """Return validated insertion kwargs, or the existing failure envelope."""
    principal = current_principal()
    if principal and principal.kind is PrincipalKind.PLAYBOOK:
        sender = playbook_sender()
        if sender is None:
            return {"success": False, "error": "unauthorized", "error_code": "unauthorized"}
        args = {**args, "from_kind": sender["from_kind"], "from_id": sender["from_id"]}
    to_kind = (args.get("to_kind") or "").strip()
    to_id = (args.get("to_id") or "").strip()
    if to_kind not in MESSAGE_TO_KINDS:
        return {
            "success": False,
            "error": f"Invalid to_kind '{to_kind}'. "
            f"Allowed: {', '.join(sorted(MESSAGE_TO_KINDS))}"
        }
    if not to_id:
        return {"success": False, "error": "to_id is required"}

    from_kind = (args.get("from_kind") or "user").strip()
    from_id = (args.get("from_id") or "").strip()

    # A task, or a session addressed by id, belongs to one project, and its
    # readers are fenced to that project: a durable message wait matches
    # only same-project rows.  When
    # the caller names no project the recipient supplies it, so the global
    # supervisor's reply lands where the waiting worker can see it.
    requested_project_id = args.get("project_id") or (
        principal.project_id if principal and principal.kind is PrincipalKind.PLAYBOOK else None
    ) or (
        None
        if args.get("system_only")
        else await recipient_project(handler, to_kind, to_id, conn)
    )
    scope = handler._current_scope or {}
    scoped_global_report = False
    if (
        to_kind == "session" and scope.get("kind") == "session"
        and scope.get("project_id") is not None
        and (to_id == "n-supervisor--global" or await handler._global_supervisor_address(to_id))
    ):
        if (
            to_id != _GLOBAL_SUPERVISOR_ADDRESS or args.get("system_only")
            or await handler._project_supervisor_session() is None
            or args.get("project_id") not in (None, scope["project_id"])
        ):
            return {"success": False, "error": "out of scope: system messages require global admin"}
        scoped_global_report = True
        requested_project_id = scope["project_id"]
        from_kind, from_id = "session", scope["session_id"]
    if not scoped_global_report and not (
        principal and principal.kind is PrincipalKind.PLAYBOOK
    ):
        sender = await handler._ordinary_message_sender(args)
        if isinstance(sender, dict):
            return {"success": False, **sender}
        from_kind, from_id = sender
    global_sender = bool(principal and principal.kind is PrincipalKind.PLAYBOOK
                         and principal.project_id is None and not requested_project_id) or (
        scope.get("kind") == "session"
        and scope.get("elevated")
        and scope.get("project_id") is None
        and not requested_project_id
    )
    system_only = global_sender or bool(args.get("system_only")) or (
        to_kind == "session" and to_id == "supervisor-global" and not scoped_global_report
    ) or (
        not requested_project_id
        and from_kind == "session"
        and from_id == "supervisor-global"
    )
    if system_only:
        scope_error = system_scope_error(handler)
        if scope_error:
            return scope_error
        project_id = None
    else:
        project_id = requested_project_id or handler._active_project_id
        if not project_id:
            return {"success": False, "error": "project_id is required (no active project set)"}
        project = await lookup(handler, "projects", project_id, conn)
        if not project:
            return {"success": False, "error": f"Project '{project_id}' not found"}

    if to_kind == "session" and to_id.startswith(_SUPERVISOR_ADDRESS_PREFIX):
        mailbox = await supervisor_mailbox(handler, to_id, project_id, conn)
        if mailbox is None:
            return {
                "success": False,
                "error": (
                    f"session:{to_id} names no project and the message has no "
                    f"project to resolve it to; send to "
                    f"session:{_SUPERVISOR_ADDRESS_PREFIX}<project-id> or "
                    f"session:{_GLOBAL_SUPERVISOR_ADDRESS}"
                )
            }
        to_id = mailbox

    denial = await authorize_submission(handler, args, to_kind, to_id, project_id, conn)
    if denial:
        return denial

    body = args.get("body")
    if not body or not str(body).strip():
        return {"success": False, "error": "body is required"}

    priority = args.get("priority", 100)
    if isinstance(priority, bool) or not isinstance(priority, int):
        return {"success": False, "error": "priority must be an integer"}

    reply_to_id = args.get("reply_to_id")
    if reply_to_id:
        parent = await lookup(handler, "messages", reply_to_id, conn)
        if parent is None:
            return {"success": False, "error": f"Message '{reply_to_id}' not found (reply_to_id)"}
        if scoped_global_report:
            if (
                parent["to_kind"] != "session"
                or parent["to_id"] not in await handler._own_supervisor_addresses()
                or parent["from_kind"] != "session"
                or not await handler._global_supervisor_address(parent["from_id"])
            ):
                return {"success": False, "error": "out of scope: reply must address an own global directive"}
        if is_collaboration_thread(parent["thread_id"]):
            return {
                "success": False,
                "error": "Collaboration replies must use their collaboration thread",
                "error_code": "collaboration.invalid",
            }

    pane_open = args.get("pane_open")
    # Private internal callers can label a message for a delivery policy
    # without exposing a new public message-send option.
    body_kind = args.get("_body_kind")
    if body_kind not in {None, "task_comment"}:
        return {"success": False, "error": "invalid internal message body kind"}
    pane_open_json: str | None = None
    if pane_open is not None:
        if not isinstance(pane_open, dict):
            return {"success": False, "error": "pane_open must be an object"}
        view_id = pane_open.get("view")
        if not view_id or not isinstance(view_id, str):
            return {"success": False, "error": "pane_open.view is required"}
        from src.panes.registry import SERVER_PANE_REGISTRY

        entry = SERVER_PANE_REGISTRY.get(view_id)
        if entry is None:
            return {"success": False, "error": f"unknown pane view '{view_id}'"}
        if not entry.agent_pushable:
            return {"success": False, "error": f"pane view '{view_id}' is not agent-pushable"}
        pane_args = pane_open.get("args", {})
        if not isinstance(pane_args, dict):
            return {"success": False, "error": "pane_open.args must be an object"}
        import json as _json

        pane_open_json = _json.dumps({"view": view_id, "args": pane_args})
        body_kind = "pane_open"

    return dict(
        project_id=project_id,
        from_kind=from_kind,
        from_id=from_id,
        to_kind=to_kind,
        to_id=to_id,
        body=str(body),
        subject=args.get("subject"),
        thread_id=args.get("thread_id"),
        priority=priority,
        archive_after_inject=bool(args.get("archive_after_inject", False)),
        reply_to_id=reply_to_id,
        body_kind=body_kind,
        pane_open=pane_open_json,
    )
