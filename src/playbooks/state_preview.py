"""Run/path-local state simulation. This module never obtains a database handle."""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from copy import deepcopy

from jsonschema import Draft202012Validator

from src.commands.playbook_state_commands import STATE_SCHEMAS
from src.observations.snapshots import PAGE_BYTES, RESULT_BYTES, encoded as page_encoded
from src.playbooks.invocation import _invocation_context, current_invocation
from src.playbooks.private_values import contains_reference
from src.playbooks.state import Limits, StateError, digest, encoded, project_entry
from src.playbooks.state import resolve_namespace_from_store
from src.playbooks.state_notify import (
    notification_request, notification_result, operation_identity, transition,
)

_context = contextvars.ContextVar("_state_preview", default=None)


class StatePreviewUnresolved(Exception):
    """A simulated operation needs production facts that preview cannot consult."""


@contextmanager
def preview_context(ctx):
    token = _context.set(ctx)
    try:
        with _invocation_context(ctx):
            yield
    finally:
        _context.reset(token)


def _recipient_known(namespace, message):
    # Without DB reads, arbitrary task/session ownership is unresolved. Logical
    # supervisor addresses and system user mailboxes can be simulated exactly.
    kind, identity = message["to_kind"], message["to_id"]
    if namespace.scope_kind == "project":
        return (kind == "session" and identity in {
            "supervisor-" + namespace.scope_owner, "n-supervisor--" + namespace.scope_owner,
        }) or (kind == "profile" and identity == "supervisor")
    return (kind == "session" and identity == "supervisor-global") or kind == "user"


async def preview_command(command, args, principal):
    ctx = _context.get()
    if ctx is None:
        return {"success": False, "error_code": "state.preview_context_required"}
    try:
        inputs = args.model_dump()
        encoded(inputs)
        if not Draft202012Validator(STATE_SCHEMAS[command]).is_valid(inputs):
            raise StateError("state.invalid_arguments")
        namespace = resolve_namespace_from_store(
            ctx.services.artifact_store, command, principal, current_invocation())
        if command == "playbook_state_notify":
            if (not principal.policy.allows_aq_command("message_send")
                    or "message_send" not in current_invocation().aq_commands):
                raise StateError("unauthorized")
            if not _recipient_known(namespace, inputs["message"]):
                raise StatePreviewUnresolved("state.preview_recipient_unresolved")
        # Stage a copy: conflicts and limits must not consume simulated state.
        staged = deepcopy(ctx.state_preview.get(namespace.identifier, {
            "revision": 0, "value_bytes": 0, "journal_bytes": 0, "key_count": 0,
            "entries": {}, "operations": {}, "pages": {}, "replay_floor": None,
        }))
        result = _apply(command, inputs, namespace, staged, ctx.services.clock())
        ctx.state_preview[namespace.identifier] = staged
        return deepcopy({**result, "preview": True})
    except StateError as exc:
        return {"success": False, "error_code": exc.code, "error": exc.code, "preview": True}


def _apply(command, args, namespace, state, now):
    limits = Limits()
    space = args.get("record_space", "kv")
    if command == "playbook_state_list":
        return _list(args, namespace, state, now)
    if command == "playbook_state_get":
        row = state["entries"].get((space, args["key"]))
        result = project_entry(row, key=args["key"], record_space=space, now=now)
        if result["exists"]:
            result["value"] = deepcopy(row["value"])
        return dict(success=True, **result)
    notify = command == "playbook_state_notify"
    request = notification_request(**args) if notify else {
        "action": command.rsplit("_", 1)[1], "key": args["key"],
        "expected_revision": args["expected_revision"], "value": args.get("value"),
        "ttl_seconds": args.get("ttl_seconds"),
    }
    operation = operation_identity(namespace, request) if notify else args["operation_key"]
    prior = state["operations"].get(operation)
    if prior:
        if prior[0] != digest(request):
            raise StateError("idempotency_conflict")
        return deepcopy(prior[1])
    identity = ("notification" if notify else "kv", args["key"])
    row = state["entries"].get(identity)
    value = request["last_value"] if notify else request["value"]
    if contains_reference(value):
        raise StateError("state.reference_input_forbidden")
    value_bytes = len(encoded(value)) if notify or request["action"] == "set" else 0
    if value_bytes > limits.value_bytes:
        raise StateError("state.value_oversize")
    if notify:
        if len(encoded(request["source_partition"])) > 4096:
            raise StateError("state.partition_oversize")
        values, due = transition(state, row, request, now)
        if due:
            values["message_id"] = "preview-message:" + digest([namespace.identifier, operation])
        value_bytes += len(encoded(values))
        result = notification_result(values, state, due, now)
    else:
        revision = row["revision"] if row else 0
        if revision != args["expected_revision"]:
            raise StateError("conflict")
        ttl = args.get("ttl_seconds")
        values = dict(record_space="kv", key=args["key"], revision=revision + 1,
                      tombstone=request["action"] == "delete", expires_at=now + ttl if ttl else None,
                      created_at=row["created_at"] if row else now, updated_at=now)
        result = dict(success=True, **project_entry(values, key=args["key"],
                                                    record_space="kv", now=now),
                      namespace_revision=state["revision"] + 1)
        result.pop("value")
    journal_bytes = len(encoded(request)) + len(encoded(result)) + len(encoded(operation))
    total = state["value_bytes"] + value_bytes - (row["value_bytes"] if row else 0)
    if (state["key_count"] + int(row is None) > limits.keys or total > limits.namespace_bytes
            or state["journal_bytes"] + journal_bytes > limits.journal_bytes):
        raise StateError("state.quota_exceeded")
    state.update(revision=state["revision"] + 1, value_bytes=total,
                 key_count=state["key_count"] + int(row is None),
                 journal_bytes=state["journal_bytes"] + journal_bytes)
    state["entries"][identity] = {**values, "value": value, "value_bytes": value_bytes}
    state["operations"][operation] = (digest(request), result)
    return result


def _list(args, namespace, state, now):
    from src.observations.snapshots import SnapshotError, validate_paging

    try:
        validate_paging({"pagination": "snapshot", **args})
    except SnapshotError as exc:
        raise StateError(exc.code) from None
    request = {"record_space": args.get("record_space", "kv"), "prefix": args.get("prefix", ""),
               "page_size": args.get("page_size", 500)}
    if args.get("snapshot_id"):
        page = state["pages"].get((args["snapshot_id"], args["page_token"]))
        if page is None or page[0] != digest(request):
            raise StateError("snapshot.token_mismatch")
        if page[1]["expires_at"] <= now:
            raise StateError("snapshot.expired")
        return deepcopy(page[1])
    rows = []
    for (space, key), row in sorted(state["entries"].items()):
        if space == request["record_space"] and key.startswith(request["prefix"]):
            result = project_entry(row, key=key, record_space=space, now=now)
            if result["exists"]:
                result["value"] = row["value"]
            rows.append(result)
    snapshot = "preview-snapshot:" + digest([namespace.identifier, request, state["revision"], now])
    manifest = [{key: row[key] for key in ("record_space", "key", "revision", "exists", "tombstone",
                "expired", "active", "source_partition") if key in row} for row in rows]
    result = dict(success=True, preview=True, complete=True, snapshot_id=snapshot,
                  namespace=namespace.as_dict(), namespace_revision=state["revision"],
                  identity_manifest=manifest, page_tokens=[], expires_at=now + 1800)
    pages, items = [], []
    for row in rows:
        if len(page_encoded({"entries": [row]})) > PAGE_BYTES:
            raise StateError("snapshot.manifest_oversize")
        if items and (len(items) >= request["page_size"]
                      or len(page_encoded({"entries": [*items, row]})) > PAGE_BYTES):
            pages.append(items)
            items = []
        items.append(row)
    if items:
        pages.append(items)
    for index, page in enumerate(pages):
        token = "preview-page:" + digest([snapshot, index])
        result["page_tokens"].append(dict(page_token=token, kind="entries", row_count=len(page)))
        state["pages"][(snapshot, token)] = (digest(request), dict(
            success=True, preview=True, entries=deepcopy(page), snapshot_id=snapshot,
            namespace_revision=state["revision"], expires_at=now + 1800))
    if len(page_encoded(result)) > RESULT_BYTES:
        raise StateError("snapshot.manifest_oversize")
    return result
