"""Input schemas for the playbook state commands — pure data, no daemon imports.

``src.tools.definitions`` imports these at CLI startup, so this module must not
reach the database, the handler or the playbook runtime.
"""
from __future__ import annotations

_TEXT = {"type": "string", "minLength": 1, "maxLength": 512}
_REVISION = {"type": "integer", "minimum": 0, "maximum": 9_223_372_036_854_775_806}
_NAMESPACE = {
    "type": "object", "additionalProperties": False,
    "required": ["scope_kind", "scope_owner", "playbook_id", "state_schema_version"],
    "properties": {"scope_kind": {"enum": ["system", "project", "profile"]},
                   "scope_owner": {**_TEXT, "maxLength": 256},
                   "playbook_id": {**_TEXT, "maxLength": 256},
                   "state_schema_version": {"type": "integer", "minimum": 1,
                                            "maximum": 2_147_483_647}},
}
_READ = {"record_space": {"enum": ["kv", "notification"]}}
_PAGES = {
    **_READ, "prefix": {"type": "string", "maxLength": 512},
    "pagination": {"enum": ["snapshot"]},
    "snapshot_id": {**_TEXT, "maxLength": 128},
    "page_token": {**_TEXT, "maxLength": 128},
    "page_size": {"type": "integer", "minimum": 1, "maximum": 500},
}


def _schema(properties, required=()):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(required)}


STATE_SCHEMAS = {
    "playbook_state_notify": _schema({
        "key": _TEXT, "action": {"enum": ["present", "cleared"]},
        "observed_at": {"type": "number"}, "observation_id": _TEXT,
        "expected_revision": _REVISION, "last_value": {},
        "source_partition": {"type": ["object", "null"]},
        "repeat_after_seconds": {"type": ["integer", "null"], "minimum": 1,
                                 "maximum": 2_147_483_647},
        "message": _schema({
            "to_kind": {"enum": ["session", "task", "profile", "user"]},
            "to_id": _TEXT,
            "body": {"type": "string", "minLength": 1, "maxLength": 16384, "pattern": r"\S"},
            "subject": {"type": ["string", "null"], "maxLength": 512},
        }, ["to_kind", "to_id", "body"]),
    }, ["key", "action", "observed_at", "observation_id", "message"]),
    "playbook_state_get": _schema({"key": _TEXT, **_READ}, ["key"]),
    "playbook_state_list": _schema(_PAGES),
    "playbook_state_set": _schema({
        "key": _TEXT, "value": {}, "expected_revision": _REVISION, "operation_key": _TEXT,
        "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 2_147_483_647},
    }, ["key", "value", "expected_revision", "operation_key"]),
    "playbook_state_delete": _schema({
        "key": _TEXT, "expected_revision": _REVISION, "operation_key": _TEXT,
    }, ["key", "expected_revision", "operation_key"]),
    "playbook_state_inspect": _schema({"namespace": _NAMESPACE, "key": _TEXT, **_PAGES},
                                      ["namespace"]),
    "playbook_state_reset": _schema({
        "namespace": _NAMESPACE, "expected_revision": _REVISION, "operation_key": _TEXT,
        "reason": {**_TEXT, "pattern": r"\S", "maxLength": 4096},
    }, ["namespace", "expected_revision", "operation_key", "reason"]),
}
