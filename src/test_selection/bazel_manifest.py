"""Decode shared Bazel input lists without changing test obligations."""

# Invalid wire values use one ValueError contract for all manifest consumers.
# ruff: noqa: TRY004

from __future__ import annotations

import hashlib
import json
import re

INPUT_FIELDS = ("source_files", "test_sources", "other_sources", "data_files")


def hydrate_manifest(document: dict) -> dict:
    """Return the logical version-one manifest, validating every version-two reference.

    Input-list hashes cover the exact ordered list, including duplicate values:
    SHA256 of compact, non-ASCII-escaped JSON encoded as UTF-8. Version one
    remains accepted unchanged for existing checkouts and recorded baselines.
    """
    if not isinstance(document, dict):
        raise ValueError("suite manifest must be an object")
    version = document.get("schema_version")
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("unsupported suite manifest version")
    if version == 1:
        return document

    sets = document.get("dependency_sets")
    if not isinstance(sets, dict):
        raise ValueError("version-two suite manifest requires dependency_sets")
    for digest, paths in sets.items():
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid dependency set hash")
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError(f"dependency set {digest} must be a list of strings")
        encoded = json.dumps(paths, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError(f"dependency set hash mismatch: {digest}")

    modules = document.get("modules")
    if not isinstance(modules, dict):
        raise ValueError("suite manifest modules must be an object")
    expanded = {}
    for path, info in modules.items():
        if not isinstance(path, str) or not isinstance(info, dict):
            raise ValueError("suite manifest module entries must be objects keyed by path")
        entry = dict(info)
        for field in INPUT_FIELDS:
            if field not in info:
                continue
            reference = info[field]
            if not isinstance(reference, dict) or set(reference) != {"input_set"}:
                raise ValueError(f"invalid dependency set reference: {path}.{field}")
            digest = reference["input_set"]
            if not isinstance(digest, str) or digest not in sets:
                raise ValueError(f"unknown dependency set reference: {path}.{field}")
            entry[field] = list(sets[digest])
        expanded[path] = entry
    result = dict(document)
    result.pop("dependency_sets")
    result.update(schema_version=1, modules=expanded)
    return result
