"""Input schemas for the doctor observation commands — pure data, no daemon imports.

``src.tools.definitions`` imports these at CLI startup, so this module must not
reach the doctor registry, the database or the handler.
"""
from __future__ import annotations

CATALOG_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
RUN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "check_id": {"type": "string", "minLength": 1},
        "group": {"type": "string", "minLength": 1},
        "project_id": {"type": ["string", "null"], "minLength": 1},
        "now": {"type": "number", "minimum": 0, "maximum": 253402300799},
        "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
        "pagination": {"type": "string", "enum": ["snapshot"]},
        "snapshot_id": {"type": "string", "minLength": 1},
        "page_token": {"type": "string", "minLength": 1},
        "page_size": {"type": "integer", "minimum": 1, "maximum": 500},
    },
    "oneOf": [{"required": ["check_id"]}, {"required": ["group"]}],
    "dependentRequired": {"page_token": ["snapshot_id", "pagination"],
                          "snapshot_id": ["page_token", "pagination"],
                          "page_size": ["pagination"]},
}
