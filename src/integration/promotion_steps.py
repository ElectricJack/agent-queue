"""Promotion-flow schema and pure validation (dev-branch releases §3.11).

The first failing layer reports all its problems. Validation never mutates
the supplied document or reads git; remote checks belong to the command.
"""

from __future__ import annotations

import copy
import fnmatch
import re
from collections import deque
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

DEFAULT_ATTESTATION = "Agent Queue Integration Attestation"
STEP_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
PLACEHOLDERS = frozenset({"version", "sha12", "utc_date", "step"})


def _object(properties: dict, *, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        **({"required": list(required)} if required else {}),
    }


def _choice(*values: str, default: str) -> dict:
    return {"type": "string", "enum": list(values), "default": default}


_TEXT = {"type": "string", "minLength": 1}
_BRANCH = {
    "type": "string",
    "minLength": 1,
    "pattern": r"^(?!refs/)(?!/)(?!.*(?:\.\.|@\{|//|[\s~^:?*\[\\]))"
    r"(?!.*(?:^|/)\.)(?!.*\.lock(?:/|$))(?!.*[/.]$)(?!@$).+$",
}
_GATE = _object(
    {
        "checks": {
            "type": "string",
            "pattern": r"^(inherit|manifest:[^\s:]+)$",
            "default": "inherit",
        },
        "attestation": {
            **_TEXT,
            "description": "Default: Agent Queue Promotion Attestation (<id>)",
        },
        "approval": _choice("operator", "requester", "none", default="operator"),
        "request_ttl": {"type": "string", "pattern": r"^[1-9][0-9]*[smhdw]$", "default": "7d"},
    }
)
_VERSIONING = _object(
    {
        "kind": _choice("none", "semver_tag", "custom", default="none"),
        "source": {"type": "string", "pattern": r"^(pyproject|package\.json|file:[^:]+:.+)$"},
        "tag_format": _TEXT,
    }
)
_VERSIONING["allOf"] = [
    {
        "if": {"properties": {"kind": {"const": "semver_tag"}}, "required": ["kind"]},
        "then": {
            "properties": {
                "tag_format": {
                    "pattern": r"^(?=.*\{version\})(?:[^{}]|\{(?:version|sha12|utc_date|step)\})+$"
                }
            }
        },
    }
]
_NOTES = _object(
    {
        "kind": _choice("none", "file_template", "changelog_heading", default="none"),
        "path": _TEXT,
        "bootstrap_sha": {
            "type": ["string", "null"],
            "pattern": r"^[0-9a-f]{40}$",
            "default": None,
        },
    }
)
_NOTES["allOf"] = [
    {
        "if": {
            "properties": {"kind": {"enum": ["file_template", "changelog_heading"]}},
            "required": ["kind"],
        },
        "then": {"required": ["path"]},
    }
]
_AFTER = _object(
    {
        "backmerge": {"type": "boolean", "default": True},
        "github_release": {"type": "boolean", "default": False},
        "deploy_hook": {
            "anyOf": [{"type": "null"}, _object({"event_type": _TEXT}, required=("event_type",))],
            "default": None,
        },
    }
)
_STEP = _object(
    {
        # The id grammar is a chain-layer refusal, rather than a schema refusal.
        "id": {"type": "string", "description": "[a-z0-9][a-z0-9-]{0,31}; unique"},
        "source": _BRANCH,
        "target": _BRANCH,
        "type": _choice("request", "continuous", default="request"),
        "gate": {**_GATE, "default": {}},
        "versioning": {**_VERSIONING, "default": {}},
        "notes": {**_NOTES, "default": {}},
        "after": {**_AFTER, "default": {}},
    },
    required=("id", "source", "target"),
)
_FLOW = {"type": ["array", "null"], "items": _STEP}
_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Agent Queue promotion flow",
    "description": "A bare flow or a document containing promotion_flow. Null disables promotions.",
    "anyOf": [_FLOW, _object({"promotion_flow": _FLOW}, required=("promotion_flow",))],
}


def _pointer(path) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


@dataclass(frozen=True)
class FlowProblem:
    code: str
    pointer: str
    message: str
    layer: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "pointer": self.pointer,
            "message": self.message,
            "layer": self.layer,
        }


@dataclass(frozen=True)
class FlowValidation:
    flow: list[dict[str, Any]] | None
    problems: tuple[FlowProblem, ...] = ()
    layer: int = 3

    @property
    def valid(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "flow": self.flow,
            "layer": self.layer,
            "problems": [problem.as_dict() for problem in self.problems],
        }


def _defaults(value: dict, schema: dict) -> None:
    for key, field in schema.get("properties", {}).items():
        if key not in value and "default" in field:
            value[key] = copy.deepcopy(field["default"])
        if isinstance(value.get(key), dict):
            _defaults(value[key], field)


def _format_fields(format_: str) -> set[str] | None:
    """Only exact documented placeholders, with no conversions or format specs."""
    fields = set(re.findall(r"\{([^{}]*)\}", format_))
    rest = re.sub(r"\{[^{}]*\}", "", format_)
    return None if "{" in rest or "}" in rest or not fields <= PLACEHOLDERS else fields


def tag_glob(format_: str) -> str:
    # {step} is fixed for a step; the spec's protection glob still widens it
    # to '*', since the creation ruleset must not cover another step's tags.
    return re.sub(r"\{[^{}]*\}", "*", format_)


def _glob_tokens(pattern: str) -> list[str]:
    tokens = []
    i = 0
    while i < len(pattern):
        end = i + 1
        if pattern[i] == "[":
            search = i + 1 + (pattern[i + 1 : i + 2] == "!")
            search += pattern[search : search + 1] == "]"
            closing = pattern.find("]", search)
            if closing != -1:
                end = closing + 1
        tokens.append(pattern[i:end])
        i = end
    return tokens


def globs_overlap(left: str, right: str) -> bool:
    """Non-empty intersection of two glob languages, including reserved families.

    Walk the product of their small glob automata. Character-class predicates
    can change only at a literal/range boundary, so representatives at each
    boundary suffice even for Unicode and negated classes.
    """
    a, b = _glob_tokens(left), _glob_tokens(right)
    alphabet = {chr(n) for n in range(128)} | set(left + right)
    alphabet |= {chr(ord(c) + 1) for c in left + right if ord(c) < 0x10FFFF}
    alphabet.add(chr(0x10FFFF))
    queue, seen = deque([(0, 0)]), set()
    while queue:
        i, j = queue.popleft()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if i == len(a) and j == len(b):
            return True
        if i < len(a) and a[i] == "*":
            queue.append((i + 1, j))
        if j < len(b) and b[j] == "*":
            queue.append((i, j + 1))
        if (
            i < len(a)
            and j < len(b)
            and any(fnmatch.fnmatchcase(c, a[i]) and fnmatch.fnmatchcase(c, b[j]) for c in alphabet)
        ):
            queue.append((i + (a[i] != "*"), j + (b[j] != "*")))
    return False


class FlowSchema:
    @staticmethod
    def schema() -> dict[str, Any]:
        """The public schema, detached so callers cannot change validation."""
        return copy.deepcopy(_SCHEMA)

    @staticmethod
    def validate(
        document: Any,
        *,
        default_branch: str,
        manifest: dict[str, Any] | None = None,
    ) -> FlowValidation:
        wrapped = isinstance(document, dict)
        prefix = ("promotion_flow",) if wrapped else ()
        if wrapped:
            structural = _object({"promotion_flow": _FLOW}, required=("promotion_flow",))
        else:
            structural = _FLOW
        problems = []
        for error in Draft202012Validator(structural).iter_errors(document):
            path = tuple(error.absolute_path)
            # Point at the actual unknown key, including RFC 6901 escaping.
            keys = [None]
            if error.validator == "additionalProperties":
                keys = sorted(set(error.instance) - set(error.schema.get("properties", {})))
            elif error.validator == "required":
                keys = [key for key in error.validator_value if key not in error.instance]
            for key in keys:
                message = (
                    f"Missing required property {key!r}."
                    if error.validator == "required"
                    else error.message
                )
                problems.append(
                    FlowProblem(
                        "flow_schema_invalid",
                        _pointer(path + (() if key is None else (key,))),
                        message,
                        1,
                    )
                )
        if problems:
            return FlowValidation(None, tuple(dict.fromkeys(problems)), 1)
        flow = copy.deepcopy(document["promotion_flow"] if wrapped else document)
        if not flow:
            return FlowValidation(flow)
        for step in flow:
            _defaults(step, _STEP)
            step["gate"].setdefault(
                "attestation", f"Agent Queue Promotion Attestation ({step['id']})"
            )
            if step["versioning"]["kind"] == "semver_tag":
                step["versioning"].setdefault("tag_format", "v{version}")

        def problem(code: str, index: int, field: str, message: str, layer: int = 2) -> None:
            problems.append(
                FlowProblem(
                    code, _pointer(prefix + (index,) + tuple(field.split("/"))), message, layer
                )
            )

        ids, targets, attestations, formats = set(), set(), {DEFAULT_ATTESTATION}, []
        trust = manifest if isinstance(manifest, dict) else {}
        tag_policy = trust.get("versioning")
        reserved = tag_policy.get("reserved_globs", []) if isinstance(tag_policy, dict) else []
        if not isinstance(reserved, list) or not all(isinstance(glob, str) for glob in reserved):
            reserved = ["*"]  # malformed reserved families cannot authorize a tag family
        for i, step in enumerate(flow):
            id_, gate, versioning = step["id"], step["gate"], step["versioning"]
            if not STEP_ID.fullmatch(id_):
                problem("step_id_invalid", i, "id", "Use [a-z0-9][a-z0-9-]{0,31}.")
            if id_ in ids:
                problem("step_id_duplicate", i, "id", "Step ids must be unique.")
            ids.add(id_)
            expected = default_branch if i == 0 else flow[i - 1]["target"]
            if step["source"] != expected:
                problem(
                    "source_not_default" if i == 0 else "chain_broken",
                    i,
                    "source",
                    f"Source must be {expected!r}.",
                )
            if step["target"] == default_branch:
                problem("target_is_default", i, "target", "Target must differ from the default.")
            if step["target"] in targets:
                problem("target_duplicate", i, "target", "Targets must be unique.")
            targets.add(step["target"])
            if gate["attestation"] in attestations:
                problem(
                    "attestation_duplicate", i, "gate/attestation", "Attestations must be distinct."
                )
            attestations.add(gate["attestation"])
            if step["type"] == "request" and gate["approval"] == "none":
                problem(
                    "approval_none_on_request", i, "gate/approval", "Requests require approval."
                )

            kind = versioning["kind"]
            format_ = versioning.get("tag_format", "")
            fields = _format_fields(format_)
            has_version = kind == "semver_tag" or (
                kind == "custom" and fields is not None and "version" in fields
            )
            if kind == "custom" and (fields is None or not fields & {"version", "sha12"}):
                problem(
                    "custom_tag_format_invalid",
                    i,
                    "versioning/tag_format",
                    "Use {version} or {sha12} and only documented placeholders.",
                )
            if has_version and not versioning.get("source"):
                problem(
                    "versioning_source_missing",
                    i,
                    "versioning/source",
                    "A version source is required.",
                )
            if step["type"] == "continuous" and has_version:
                problem(
                    "continuous_versioned",
                    i,
                    "versioning/kind",
                    "Continuous steps cannot need a prepare.",
                )
            if kind != "none" and fields is not None:
                glob = tag_glob(format_)
                if any(globs_overlap(glob, other) for other in [*formats, *reserved]):
                    problem(
                        "tag_format_collision", i, "versioning/tag_format", "Tag families overlap."
                    )
                formats.append(glob)
            if step["notes"]["kind"] != "none" and not has_version:
                problem(
                    "notes_without_version", i, "notes/kind", "Notes require a version placeholder."
                )
            if step["after"]["github_release"] and kind == "none":
                problem(
                    "github_release_without_versioning",
                    i,
                    "after/github_release",
                    "A release requires a tag.",
                )
        if problems:
            return FlowValidation(flow, tuple(problems), 2)
        for i, step in enumerate(flow):
            gate = step["gate"]
            names = trust.get("promotion_attestation_names")
            if not isinstance(names, list) or gate["attestation"] not in names:
                problem(
                    "attestation_not_in_manifest",
                    i,
                    "gate/attestation",
                    "Attestation is not trusted by the manifest.",
                    3,
                )
            checks = gate["checks"]
            sets = trust.get("check_sets")
            if checks.startswith("manifest:") and (
                not isinstance(sets, dict) or checks[9:] not in sets
            ):
                problem(
                    "check_set_unknown", i, "gate/checks", "Check set is not in the manifest.", 3
                )
        return FlowValidation(flow, tuple(problems), 3)
