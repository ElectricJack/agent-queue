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


def _fill_defaults(step: dict) -> None:
    _defaults(step, _STEP)
    step["gate"].setdefault("attestation", f"Agent Queue Promotion Attestation ({step['id']})")
    if step["versioning"]["kind"] == "semver_tag":
        step["versioning"].setdefault("tag_format", "v{version}")


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
            _fill_defaults(step)

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


# A step with an open promotion keeps these; ``after`` and the gate's
# ``request_ttl`` may change at any time (§3.11).
_PROTECTED = ("source", "target", "type", "gate", "versioning", "notes")


def _filled(step: Any) -> dict[str, Any] | None:
    if not isinstance(step, dict) or not isinstance(step.get("id"), str):
        return None
    step = copy.deepcopy(step)
    try:
        _fill_defaults(step)
    except (AttributeError, KeyError, TypeError):
        pass
    if isinstance(step.get("gate"), dict):
        step["gate"].pop("request_ttl", None)
    return step


def protected_changes(stored: Any, flow: Any) -> list[dict[str, Any]]:
    """Each stored step the new flow removes or changes in a protected field.

    Steps match by id, so renaming a step removes it. Appended steps and
    ``after`` edits are never listed.
    """
    current = {}
    for index, step in enumerate(flow or ()):
        filled = _filled(step)
        if filled is not None:
            current.setdefault(filled["id"], (index, filled))
    changes = []
    for index, step in enumerate(stored or ()):
        old = _filled(step)
        if old is None:
            continue
        change = {"step_id": old["id"], "target": old.get("target")}
        if old["id"] not in current:
            changes.append({**change, "field": "id", "pointer": f"/{index}/id"})
            continue
        new_index, new = current[old["id"]]
        changes.extend(
            {**change, "field": field, "pointer": f"/{new_index}/{field}"}
            for field in _PROTECTED
            if old.get(field) != new.get(field)
        )
    return changes


def flow_targets(flow: Any) -> list[str]:
    """Target branch names in chain order."""
    if not isinstance(flow, list):
        return []
    return [
        step["target"]
        for step in flow or ()
        if isinstance(step, dict) and isinstance(step.get("target"), str)
    ]


def promotion_rulesets(
    flow: list[dict] | None, *, default_branch: str, app_id: int
) -> list[dict]:
    """The admin-owned branch and tag rulesets of revision 3 §3.8, in chain order.

    Accept a normalized, validated flow. A bypass covers an entire ruleset,
    so tag creation and immutability must always be separate.
    """
    branches = [(f"Train-only {default_branch}", default_branch, DEFAULT_ATTESTATION)]
    branches += [
        (f"Promotion-only {step['target']}", step["target"], step["gate"]["attestation"])
        for step in flow or ()
    ]

    def ruleset(name: str, target: str, ref: str, rules: list[dict], bypass: list) -> dict:
        return {
            "name": name, "target": target, "enforcement": "active",
            "conditions": {"ref_name": {"include": [ref], "exclude": []}},
            "rules": rules, "bypass_actors": bypass,
        }

    result = [
        ruleset(name, "branch", f"refs/heads/{branch}", [
            {"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": False,
                "do_not_enforce_on_create": False,
                "required_status_checks": [{"context": attestation, "integration_id": app_id}],
            }},
            {"type": "deletion"}, {"type": "non_fast_forward"},
        ], [])
        for name, branch, attestation in branches
    ]
    for step in flow or ():
        versioning = step["versioning"]
        if versioning["kind"] == "none":
            continue
        ref = "refs/tags/" + tag_glob(versioning["tag_format"])
        result += [
            ruleset(f"Promotion-tag creation ({step['id']})", "tag", ref,
                    [{"type": "creation"}],
                    [{"actor_id": app_id, "actor_type": "Integration", "bypass_mode": "always"}]),
            ruleset(f"Promotion-tag immutability ({step['id']})", "tag", ref,
                    [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}], []),
        ]
    return result


async def read_stored_promotion_flow(db: Any, project_id: str) -> Any:
    """Read the project column; lightweight injected databases may omit an engine."""
    from sqlalchemy import select

    from src.database.tables import projects

    if getattr(db, "_engine", None) is None:
        return None
    async with db._engine.connect() as conn:
        return (await conn.execute(
            select(projects.c.promotion_flow).where(projects.c.id == project_id)
        )).scalar_one()


def promotion_workflow_triggers(flow: list[dict] | None, *, default_branch: str) -> dict:
    """Copyable workflow event configuration derived from the same chain as protection."""
    branches = [default_branch, *flow_targets(flow)]
    return {
        ".github/workflows/tests.yml": {
            "pull_request": {
                "branches": branches,
                "types": ["opened", "synchronize", "reopened", "ready_for_review"],
            },
            "push": {"branches": ["aq/parent/**", "aq/integration/**",
                                   "aq/batches/**", "aq/promote/**"]},
        },
        ".github/workflows/main-attestation.yml": {"push": {"branches": branches}},
    }


async def read_promotion_protection(
    client: Any, binding: Any, flow: list[dict] | None, *, default_branch: str,
    app_id: int, policy: Any = None,
) -> dict:
    """Read every chain branch and tag pair, never issuing a GitHub write.

    A hidden bypass list cannot prove tag immutability or exclusive creation.
    Such a pair is reported as unverifiable, never as an empty actor list.
    """
    from src.integration.protection import MAX_RULE_PAGES, read_protection

    expected = promotion_rulesets(flow, default_branch=default_branch, app_id=app_id)
    branches = []
    for item in expected:
        if item["target"] != "branch":
            continue
        branch = item["conditions"]["ref_name"]["include"][0].removeprefix("refs/heads/")
        attestation = item["rules"][0]["parameters"]["required_status_checks"][0]["context"]
        reading = await read_protection(
            client, binding, branch, app_id=app_id, policy=policy, attestation_name=attestation,
        )
        branches.append({"branch": branch, "attestation": attestation, **reading.as_dict()})
    warnings = []

    def warn(code: str, message: str, **evidence: Any) -> None:
        warnings.append({"code": code, "pointer": "", "layer": 4,
                         "message": message, **evidence})

    try:
        root = f"/repositories/{binding.repository_id}"
        summaries = await client.paged_list(
            f"{root}/rulesets?includes_parents=true&per_page=100", max_pages=MAX_RULE_PAGES,
        )
        documents = []
        for summary in summaries:
            if not isinstance(summary, dict) or type(summary.get("id")) is not int:
                raise ValueError("a ruleset summary has no numeric id")
            document = await client.request_json("GET", f"{root}/rulesets/{summary['id']}")
            if document.get("id") != summary["id"]:
                raise ValueError("a ruleset answered another id")
            documents.append(document)
    except Exception as exc:  # noqa: BLE001 - provider boundary; unreadable is not missing
        warn("ruleset_unverifiable", f"Remote rulesets could not be read: {type(exc).__name__}.")
        return {"branches": branches, "tags": [], "warnings": warnings}

    tags = []
    for item in expected:
        # Names identify the admin's copyable rulesets; semantic fields are
        # compared too, so a disabled or repurposed namesake never passes.
        candidates = [doc for doc in documents if doc.get("name") == item["name"]]
        classification = "unverifiable"
        if not candidates:
            warn("ruleset_missing", f"Missing ruleset {item['name']!r}.", ruleset=item["name"])
            classification = "missing"
        elif len(candidates) != 1:
            warn("ruleset_check_mismatch", f"Ambiguous ruleset {item['name']!r}.",
                 ruleset=item["name"])
        else:
            observed = candidates[0]
            # Order of rules and actors is immaterial. Unknown parameters or
            # extra rules are drift; server-supplied metadata is ignored.
            def canonical(entries: Any) -> Any:
                import json

                return sorted(json.dumps(entry, sort_keys=True) for entry in entries) \
                    if isinstance(entries, list) else None

            mismatch = any(observed.get(key) != item[key]
                           for key in ("target", "enforcement", "conditions"))
            mismatch |= canonical(observed.get("rules")) != canonical(item["rules"])
            if "bypass_actors" in observed:
                mismatch |= canonical(observed["bypass_actors"]) != canonical(item["bypass_actors"])
            if mismatch:
                classification = "mismatched"
                warn("ruleset_check_mismatch", f"Ruleset {item['name']!r} differs from the flow.",
                     ruleset=item["name"], ruleset_id=observed["id"])
            elif "bypass_actors" not in observed:
                warn("ruleset_unverifiable", f"Bypass actors of {item['name']!r} are hidden.",
                     ruleset=item["name"], ruleset_id=observed["id"])
            elif item["target"] == "tag":
                classification = "tag_create_app_only" if item["bypass_actors"] else "tag_immutable"
            else:
                classification = "attested_only"
        if item["target"] == "tag":
            tags.append({"ruleset": item["name"], "classification": classification,
                         "ref": item["conditions"]["ref_name"]["include"][0]})
    for branch in branches:
        if branch["classification"] != "attested_only":
            code = "ruleset_unverifiable" if branch["classification"] == "unverifiable" \
                else "ruleset_check_mismatch"
            warn(code, f"Branch {branch['branch']!r} is "
                 f"{branch['classification']}, expected attested_only.", branch=branch["branch"])
    return {"branches": branches, "tags": tags, "warnings": warnings}


def _workflow_accepts(event: Any, branch: str) -> bool:
    """GitHub's ordered positive/negative branch filters, with slash-aware stars."""
    if event is None:
        return True
    if not isinstance(event, dict) or event.get("paths") or event.get("paths-ignore"):
        return False  # Required checks must run for every promotion source tree.

    def matches(pattern: str) -> bool:
        pattern = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        pattern = pattern.replace(r"\?", "[^/]")
        return re.fullmatch(pattern, branch) is not None

    include = event.get("branches")
    excluded = event.get("branches-ignore", [])
    if not isinstance(excluded, list) or not all(isinstance(p, str) for p in excluded):
        return False
    if any(matches(pattern) for pattern in excluded):
        return False
    if include is None:
        return not event.get("tags") and not event.get("tags-ignore")
    if not isinstance(include, list):
        return False
    accepted = False
    for pattern in include:
        if not isinstance(pattern, str):
            return False
        if matches(pattern.removeprefix("!")):
            accepted = not pattern.startswith("!")
    return accepted


async def validate_promotion_remote(
    client: Any, binding: Any, flow: list[dict] | None, *, default_branch: str, app_id: int,
) -> dict:
    """Layer-four ruleset and workflow warnings at the default's exact remote SHA."""
    import yaml

    from src.integration.app_mode import read_file_at

    report = await read_promotion_protection(
        client, binding, flow, default_branch=default_branch, app_id=app_id,
    )
    expected = promotion_workflow_triggers(flow, default_branch=default_branch)
    warnings = report["warnings"]
    try:
        sha = await client.exact_head_ref(default_branch)
        if not sha:
            raise ValueError("the default branch is missing")
        for path, events in expected.items():
            raw = await read_file_at(client, binding, path, sha)
            if raw is None and path.endswith("main-attestation.yml"):
                raw = await read_file_at(client, binding, ".github/workflows/unattested-push.yml", sha)
            document = yaml.safe_load(raw) if raw is not None else {}
            triggers = document.get("on", document.get(True, {})) if isinstance(document, dict) else {}
            for event, settings in events.items():
                configured = triggers.get(event) if isinstance(triggers, dict) else None
                present = event in triggers if isinstance(triggers, (dict, list)) else triggers == event
                for branch in settings["branches"]:
                    # Use a concrete nested ref to require ** rather than *.
                    sample = branch.replace("**", "step/request")
                    types = configured.get("types") if isinstance(configured, dict) else None
                    if event == "pull_request" and types is None:
                        types = ["opened", "synchronize", "reopened"]  # GitHub's default activity types.
                    lifecycle = event != "pull_request" or (
                        isinstance(types, list) and set(settings["types"]).issubset(types)
                    )
                    if not present or not lifecycle or not _workflow_accepts(configured, sample):
                        warnings.append({"code": "workflow_trigger_missing", "pointer": "",
                                         "layer": 4, "message": f"{path} lacks {event} for {branch}.",
                                         "workflow": path, "event": event, "branch": branch})
    except Exception as exc:  # noqa: BLE001 - unreadable workflow is never success
        warnings.append({"code": "workflow_unverifiable", "pointer": "", "layer": 4,
                         "message": f"Workflow triggers could not be read: {type(exc).__name__}."})
    return {"protection": {key: report[key] for key in ("branches", "tags")},
            "workflow_triggers": expected, "warnings": warnings}


def flow_status(
    document: Any, *, default_branch: str | None, recorded: Any = ()
) -> dict[str, Any] | None:
    """The chain ``aq integration status`` prints, re-validated on read (R17).

    Layers 1-2 run here against the current default branch, so a fix to
    either side shows at once. ``recorded`` carries the daemon-start result;
    only its layer-3 findings, which need the trust manifest, are kept, and
    they clear on reactivation or the next daemon start. Any problem marks
    every target ``misconfigured``; the train ignores such a flow and promotes
    to the default branch. Nothing is written.
    """
    if not document:
        return None
    result = FlowSchema.validate(document, default_branch=default_branch or "")
    problems = [problem.as_dict() for problem in result.problems if problem.layer < 3]
    problems = problems or [
        dict(problem) for problem in recorded or () if problem.get("layer") not in (1, 2)
    ]
    raw = document.get("promotion_flow") if isinstance(document, dict) else document
    candidates = result.flow if result.flow is not None else raw
    steps = [step for step in candidates if isinstance(step, dict)] if isinstance(
        candidates, list
    ) else []
    state = "misconfigured" if problems else "configured"
    pairs = [(str(step.get("source")), str(step.get("target"))) for step in steps]
    linked = all(pairs[i][0] == pairs[i - 1][1] for i in range(1, len(pairs)))
    chain = (
        " -> ".join([pairs[0][0], *(target for _source, target in pairs)])
        if pairs and linked
        else "; ".join(f"{source} -> {target}" for source, target in pairs)
    )
    return {
        "state": state,
        "chain": chain,
        "steps": [
            {
                "id": step.get("id"),
                "source": step.get("source"),
                "target": step.get("target"),
                "target_ref": f"refs/heads/{step.get('target')}",
                "type": step.get("type", "request"),
                "state": state,
            }
            for step in steps
        ],
        "problems": problems,
    }


async def recheck_stored_flows(db: Any, validate: Any) -> dict[str, list[dict[str, Any]]]:
    """Re-run layers 1-3 on every stored flow at daemon start (R17).

    ``validate(project_id)`` is the ``promote_validate`` command. Returns the
    problems per project whose flow no longer validates; nothing is written.
    An unreadable trust manifest is not a manifest change, so it alone does
    not mark a flow, and a flow replaced while the check ran is dropped: its
    activation validated layers 1-3 itself.
    """
    from sqlalchemy import select

    from src.database.tables import projects

    async def stored_flows(ids=None):
        query = select(projects.c.id, projects.c.promotion_flow).where(
            projects.c.promotion_flow.is_not(None)
        )
        if ids is not None:
            query = query.where(projects.c.id.in_(ids))
        async with db._engine.connect() as conn:
            return (await conn.execute(query)).all()

    rows = await stored_flows()
    checked = dict(rows)
    found = {}
    for project_id, document in rows:
        if not document:
            continue
        try:
            result = await validate(project_id)
        except Exception as exc:  # noqa: BLE001 - one project's failure must not stop the rest
            result = {"error": f"{type(exc).__name__}: {exc}"}
        if result.get("outcome") == "valid":
            continue
        problems = list(result.get("problems") or ())
        if any(w.get("code") == "trust_manifest_unavailable" for w in result.get("warnings") or ()):
            problems = [problem for problem in problems if problem.get("layer", 0) < 3]
            if not problems:
                continue
        found[project_id] = problems or [
            {
                "code": "flow_check_failed",
                "pointer": "",
                "message": str(result.get("error") or "flow did not validate"),
                "layer": 0,
            }
        ]
    if found:
        current = dict(await stored_flows(list(found)))
        found = {pid: problems for pid, problems in found.items()
                 if current.get(pid) == checked[pid]}
    return found


async def create_missing_targets(
    git: Any, checkout: str, flow: list[dict[str, Any]], *, repository_url: str | None = None
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """Create each absent target at its source's tip with a create-only push.

    Layer 4 at activation (§3.11): an existing target must be an ancestor of,
    or equal to, its source, and an absent one is created in chain order, so a
    step may take its source from the target the step before just created.
    Returns ``(refusal, created)``: the refusal, or ``None`` once every target
    exists, and each branch this call created with its OID. No existing head
    ever moves, and a lost race or failed push refuses rather than retries.
    """
    from src.git.manager import RemoteRefState

    def refusal(error: str, index: int, field: str, message: str, **extra: Any) -> dict:
        return {"outcome": "invalid", "error": error, "pointer": f"/{index}/{field}",
                "message": message, "layer": 4, **extra}

    branches = list(dict.fromkeys(b for step in flow for b in (step["source"], step["target"])))
    remote = await git.als_remote_refs(checkout, branches, repository_url=repository_url)
    unreadable = [b for b in branches if remote[b].state is RemoteRefState.ERROR]
    if unreadable:
        return {"outcome": "blocked", "error": "remote_unavailable", "branches": unreadable,
                "message": remote[unreadable[0]].error or "cannot read the remote heads"}, {}
    oids = {b: remote[b].oid for b in branches if remote[b].state is RemoteRefState.PRESENT}
    created: dict[str, str] = {}
    fetched = False
    for index, step in enumerate(flow):
        source, target = step["source"], step["target"]
        if source not in oids:
            return refusal("source_missing_on_remote", index, "source",
                           f"Source branch {source!r} does not exist on the remote."), {}
        if target not in oids:
            oids[target] = created[target] = oids[source]
            continue
        if oids[target] == oids[source]:
            continue
        if not fetched:
            await git.afetch_origin(checkout, repository_url=repository_url or "", all_heads=True)
            fetched = True
        ancestor = await git.ais_ancestor(checkout, oids[target], oids[source], strict=True)
        if ancestor is None:
            return {"outcome": "blocked", "error": "ancestry_unknown", "branches": [target, source],
                    "message": f"Could not compare {target!r} with {source!r}."}, {}
        if not ancestor:
            return refusal("target_not_ancestor_of_source", index, "target",
                           f"Target {target!r} is not an ancestor of, or equal to, {source!r}.",
                           target_oid=oids[target], source_oid=oids[source]), {}
    if not created:
        return None, {}
    after = await git.apush_new_refs(checkout, created, repository_url=repository_url)
    landed = {b: o for b, o in created.items()
              if (r := after.get(b)) is not None and r.state is RemoteRefState.PRESENT
              and r.oid == o}
    for index, step in enumerate(flow):
        target = step["target"]
        if target not in created:
            continue
        result = after.get(target)
        if result is None or result.state is not RemoteRefState.PRESENT or (
            result.oid != created[target]
        ):
            return refusal("target_create_failed", index, "target",
                           (result.error if result is not None else None)
                           or f"Target {target!r} was not created at its source's tip."), landed
    return None, landed
