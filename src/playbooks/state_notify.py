"""Atomic notification episodes using the existing state journal and message queue."""
from __future__ import annotations

from src.database.queries import playbook_state_queries as queries
from src.messages.submission import playbook_sender, prepare_submission
from src.playbooks.private_values import contains_reference
from src.playbooks.state import StateError, StateService, digest, encoded, project_entry


def notification_request(*, key, action, observed_at, observation_id, message,
                         expected_revision=None, last_value=None, source_partition=None,
                         repeat_after_seconds=None):
    # The command schema validates shapes first. Normalize optional fields for replay.
    if action == "cleared" and expected_revision is None:
        raise StateError("state.invalid_arguments")
    if contains_reference(last_value) or contains_reference(source_partition):
        raise StateError("state.reference_input_forbidden")
    return dict(key=key, action=action, observed_at=float(observed_at),
                observation_id=observation_id, message=message,
                expected_revision=expected_revision, last_value=last_value,
                source_partition=source_partition, repeat_after_seconds=repeat_after_seconds)


def operation_identity(namespace, request):
    return "notify:" + digest([namespace.identifier, request["key"],
                               request["observation_id"], request["action"]])


def transition(locked, row, request, now):
    """Pure episode logic shared by live transactions and run-local previews."""
    observed = request["observed_at"]
    queries.check_observation_floor(locked, row, observed)
    if row and row["last_observed_at"] is not None:
        if observed < row["last_observed_at"]:
            raise StateError("state.stale_observation")
        # Identical requests were handled by the journal. Any remaining equal
        # watermark (including a changed action on the same observation) conflicts.
        if observed == row["last_observed_at"]:
            raise StateError("conflict")
    revision = row["revision"] if row else 0
    present = request["action"] == "present"
    if not present and request["expected_revision"] != revision:
        raise StateError("conflict")
    active = bool(row and row["active"])
    interval = request["repeat_after_seconds"]
    due = (not active or (interval is not None and row["last_notified"] is not None
                         and now >= row["last_notified"] + interval)) if present else active
    values = dict(
        record_space="notification", key=request["key"], revision=revision + 1,
        tombstone=not present, expires_at=None,
        episode=(row["episode"] if row else 0) + int(present and not active),
        active=present, source_partition=request["source_partition"],
        first_seen=observed if present and not active else (row["first_seen"] if row else None),
        last_seen=observed if present else (row["last_seen"] if row else None),
        last_notified=now if due else (row["last_notified"] if row else None),
        last_observation_id=request["observation_id"], last_observed_at=observed,
        message_id=row["message_id"] if row else None,
        replay_floor=row["replay_floor"] if row else None,
        created_at=row["created_at"] if row else now, updated_at=now,
    )
    return values, due


def notification_result(values, locked, due, now):
    result = dict(success=True, **project_entry(values, key=values["key"],
                                               record_space="notification", now=now),
                  enqueued=due, namespace_revision=locked["revision"] + 1)
    result.pop("value")
    return result


class NotificationService(StateService):
    async def notify(self, handler, namespace, principal, **args):
        request = notification_request(**args)
        value_bytes = len(encoded(request["last_value"]))
        if value_bytes > self.limits.value_bytes:
            raise StateError("state.value_oversize")
        if len(encoded(request["source_partition"])) > 4096:
            raise StateError("state.partition_oversize")
        operation = operation_identity(namespace, request)
        self.private.require_ready()
        message = None
        async with queries.locked_namespace(self.engine, namespace) as (conn, locked):
            # Do this even for replay and suppressed calls. A revoked recipient
            # is never authorized by an old successful notification.
            submission = await prepare_submission(handler, {
                **request["message"],
                "project_id": principal.project_id,
            }, conn=conn)
            if submission.get("success") is False:
                raise StateError(submission.get("error_code", "state.invalid_message"))
            sender = playbook_sender(namespace)
            if sender is None:
                raise StateError("unauthorized")
            submission.update(sender)
            replay = await self.replay(conn, namespace, principal, operation, request)
            if replay is not None:
                return replay
            row = await queries.entry_row(conn, namespace, "notification", request["key"], lock=True)
            now = await queries.transaction_time(conn)
            values, due = transition(locked, row, request, now)
            if due:
                message = await handler.db.create_message_in_transaction(conn, **submission)
                values["message_id"] = message.id
            payload_bytes = value_bytes + len(encoded(values))
            batch = self.private.batch(namespace.owner)
            values.update(value_reference=batch.protect(request["last_value"]),
                          value_bytes=payload_bytes)
            result = notification_result(values, locked, due, now)
            await queries.write_entry(conn, namespace, values)
            await queries.commit_operation(
                conn, namespace, locked, operation_key=operation, request_digest=digest(request),
                result_reference=batch.protect(result), result=result,
                journal_bytes=len(encoded(request)) + len(encoded(result)) + len(encoded(operation)),
                value_bytes=locked["value_bytes"] + payload_bytes - (row["value_bytes"] if row else 0),
                key_count=locked["key_count"] + int(row is None), limits=self.limits, now=now,
            )
            await batch.persist(conn)
        if message is not None:
            # This is only a wake hint. The existing MessageDeliveryService reads
            # committed backlog after restart or a failed emit; no new queue loop.
            await handler._emit_message_event("message.sent", {
                "message_id": message.id, "project_id": message.project_id,
                "from_kind": message.from_kind, "from_id": message.from_id,
                "to_kind": message.to_kind, "to_id": message.to_id,
                "thread_id": message.thread_id, "subject": message.subject,
            })
        return result
