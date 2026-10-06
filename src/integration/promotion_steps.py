"""Promotion-flow schema and pure validation (dev-branch releases §3.11).

The first failing layer reports all its problems. Validation never mutates
the supplied document or reads git; remote checks belong to the command.
"""

from __future__ import annotations

import copy
import fnmatch
import re
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from datetime import UTC, datetime
from urllib.parse import quote

from jsonschema import Draft202012Validator

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import (
    integration_batch_members, integration_batches, task_context, task_metadata, tasks,
)
from src.git.manager import GitError, RemoteRefState, is_valid_git_oid
from src.git.github_contracts import GitHubAccessError
from src.integration.batches import BatchObservation, BatchService
from src.integration.ci import (
    AttestationPayload, AuthenticatedGitHubObserver, IntegrationTrustManifest,
    PromotionAttestationPayload, TrustedCIObservation,
)
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.subjects import HeadIdentity

log = logging.getLogger(__name__)
PROMOTION_CONTEXT = "promotion_intent"
PROMOTION_RESULT = "promotion_result"

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
        "operator_logins": {
            "type": "array", "uniqueItems": True,
            "items": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$"},
            "description": "Optional narrowing allowlist: a listed reviewer must still be "
            "a human repository administrator.",
        },
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


class PromotionIntentInvalid(ValueError):
    """A requested step no longer names its frozen, retained source."""


class StepAdmission:
    """Promotion identity is independent of worker-completion eligibility.

    The request command stores its metadata in one existing task_context row
    (type=promotion_intent); batch.policy_snapshot['promotion_step'] is the
    independent frozen step copy. No new table or mutable completion record.
    """

    def __init__(self, db, gitops, step: dict):
        self.db, self.gitops, self.step = db, gitops, copy.deepcopy(step)

    async def load(self, batch, members, *, allow_completed=False) -> dict:
        if len(members) != 1:
            raise PromotionIntentInvalid("promotion must carry exactly one member")
        member = members[0]
        async with self.db._engine.connect() as conn:
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch.id,
            ))).mappings().one_or_none()
            task = (await conn.execute(select(tasks).where(
                tasks.c.id == member.task_id,
            ))).mappings().one_or_none()
            contexts = (await conn.execute(select(task_context.c.content).where(
                task_context.c.task_id == member.task_id, task_context.c.type == PROMOTION_CONTEXT,
            ))).scalars().all()
        try:
            if row is None or task is None or len(contexts) != 1:
                raise ValueError("missing or duplicate promotion identity")
            meta = json.loads(contexts[0])
            step = meta["step"]
            pinned_step = row["policy_snapshot"]["promotion_step"]
            pinned_intent = row["policy_snapshot"]["promotion_intent"]
            expected = (batch.project_id, batch.repository_id, batch.target_ref)
            if (row["trigger"] != "promotion" or member.order != 0
                    or (row["project_id"], row["repository_id"], row["target_ref"]) != expected
                    or (task["project_id"], task["repo_id"]) != expected[:2]
                    or task["task_type"] != "promotion" or task["profile_id"] is not None
                    or task["status"] not in ({"IN_PROGRESS", "COMPLETED"} if allow_completed
                                              else {"IN_PROGRESS"})
                    or (not allow_completed and row["intent"] != "open")
                    or meta != pinned_intent
                    or not meta.get("requester")
                    or "notes_sha256" not in meta
                    or (meta["notes_sha256"] is not None and re.fullmatch(
                        r"[0-9a-f]{64}", meta["notes_sha256"]) is None)
                    or (step["notes"]["kind"] != "none" and meta["notes_sha256"] is None)
                    or meta["request_id"] != row["request_id"]
                    or not row["request_id"].startswith(
                        f"promotion:{batch.repository_id}:{step['id']}:"
                    )
                    or meta["repository_id"] != batch.repository_id
                    or meta["target_ref"] != batch.target_ref
                    or meta["source_sha"] != member.source_sha
                    or meta["base_sha"] != member.base_sha
                    or step != pinned_step
                    or step["id"] != self.step["id"]
                    or step["source"] != self.step["source"]
                    or "refs/heads/" + step["target"] != batch.target_ref
                    or step["target"] != self.step["target"]):
                raise ValueError("promotion metadata differs from frozen inputs")
            repo = await self.gitops.repository(batch)
            identity = CompletionIdentity(
                batch.project_id, batch.repository_id, member.task_id,
                "promotion:" + meta["request_id"],
            )
            repository_url = f"https://github.com/{repo.binding.full_name}.git"
            retained = await self.gitops.git.als_remote_ref(
                str(repo.store), identity.branch, repository_url=repository_url,
            )
            if retained.state is RemoteRefState.ERROR:
                raise GitError(retained.error or "promotion retention cannot be observed")
            if retained.state is not RemoteRefState.PRESENT:
                raise ValueError("promotion source retention is absent")
            present = await self.gitops.git.arun_git_result(
                ["cat-file", "-e", retained.oid], cwd=str(repo.store),
            )
            if present.returncode:
                await self.gitops.git.afetch_repository_oid(
                    str(repo.store), repository=repo.binding, oid=retained.oid,
                    destination_ref="refs/aq/promotion-sources/" + retained.oid,
                )
            record = await GitProvenance(
                self.gitops.git, str(repo.store), repository_url=repo.binding.clone_url
                if hasattr(repo.binding, "clone_url") else f"https://github.com/{repo.binding.full_name}.git",
            ).read_completion(identity, refs={"refs/remotes/origin/" + identity.branch: retained.oid})
            if record is None or record["source_oid"] != member.source_sha:
                raise ValueError("promotion source is not retained under its request generation")
            return meta
        except (KeyError, TypeError, ValueError) as exc:
            raise PromotionIntentInvalid(str(exc)) from exc

    async def eligible(self, batch, members) -> bool:
        try:
            await self.load(batch, members)
            return True
        except (PromotionIntentInvalid, GitError, OSError):
            return False

    async def retain(self, batch, member, request_id: str) -> str:
        repo = await self.gitops.repository(batch)
        await self.gitops.exact(repo, member.source_sha)
        if not await self.gitops.is_ancestor(repo, member.base_sha, member.source_sha):
            raise PromotionIntentInvalid("promotion base is not source history")
        return await GitProvenance(
            self.gitops.git, str(repo.store),
            repository_url=f"https://github.com/{repo.binding.full_name}.git",
        ).write_completion(CompletedSource(CompletionIdentity(
            batch.project_id, batch.repository_id, member.task_id, "promotion:" + request_id,
        ), member.source_sha))


def promotion_ref(step: dict, meta: dict) -> str:
    prefix = f"promotion:{meta['repository_id']}:{step['id']}:"
    request_id = meta["request_id"]
    if not request_id.startswith(prefix) or not request_id[len(prefix):]:
        raise PromotionIntentInvalid("promotion request identity is invalid")
    suffix = request_id[len(prefix):]
    # Custom tag versions may contain '/', but never arbitrary Git revision syntax.
    ref = f"refs/heads/aq/promote/{step['id']}/{suffix}"
    from src.integration.gitops import branch

    branch(ref)
    return ref


class StepPullRequestGate:
    """Observe the request's exact PR outside the managed publication fence.

    Checks still have one producer interface, HostedChecks. This observer
    supplies only PR identity and human approval, never synthetic CI evidence.
    The visit refreshes it before publishing; the bounded fence reads that
    visit's verdict while rechecking the immutable local intent.
    """

    def __init__(self, client, binding, *, is_operator=None):
        self.client, self.binding = client, binding
        self.is_operator = is_operator or self._repository_operator

    async def _repository_operator(self, login):
        permission = await self.client.request_json(
            "GET", f"/repositories/{self.binding.repository_id}/collaborators/"
            f"{quote(login, safe='')}/permission",
        )
        if not isinstance(permission, dict) or not isinstance(permission.get("user"), dict):
            raise ValueError("operator permission response is malformed")
        user = permission["user"]
        if (not isinstance(user.get("login"), str) or not user["login"]
                or not isinstance(permission.get("permission"), str)):
            raise ValueError("operator permission identity is missing")
        return (user["login"].casefold() == login.casefold()
                and user.get("type") == "User"
                and permission.get("permission") == "admin")

    async def observe(self, batch, meta):
        number = meta.get("pr_number")
        if type(number) is not int or number <= 0:
            return "promotion_pr_missing"
        expected_url = f"https://github.com/{self.binding.full_name}/pull/{number}"
        if meta.get("pr_url") != expected_url:
            return "promotion_pr_identity_mismatch"
        try:
            pull = await self.client.request_json(
                "GET", f"/repositories/{self.binding.repository_id}/pulls/{number}",
            )
            head, base = pull.get("head") or {}, pull.get("base") or {}
            identity = {"id": self.binding.repository_id, "full_name": self.binding.full_name}
            if (pull.get("number") != number or pull.get("html_url") != expected_url
                    or any((side.get("repo") or {}).get(key) != value
                           for side in (head, base) for key, value in identity.items())
                    or head.get("ref") != promotion_ref(meta["step"], meta).removeprefix("refs/heads/")
                    or head.get("sha") != meta["source_sha"]
                    or base.get("ref") != batch.target_ref.removeprefix("refs/heads/")):
                return "promotion_pr_identity_mismatch"
            if pull.get("state") != "open":
                return "promotion_pr_closed"
            if pull.get("draft") is not False:
                return "promotion_pr_draft"
            approval = meta["step"]["gate"]["approval"]
            if approval == "none":
                return None
            reviews = await self.client.paged_list(
                f"/repositories/{self.binding.repository_id}/pulls/{number}/reviews?per_page=100",
            )
            latest = {}
            for review in sorted(reviews, key=lambda item: item.get("id", 0)):
                if type(review.get("id")) is not int or review["id"] <= 0:
                    return "promotion_pr_reviews_unavailable"
                user = review.get("user") or {}
                login = user.get("login")
                if (user.get("type") != "User" or not isinstance(login, str) or not login
                        or review.get("state") not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}):
                    continue
                # A newer dismissal or review on another head invalidates the
                # reviewer's old approval; comments alone do not replace it.
                latest[login.casefold()] = review
            if any(r.get("commit_id") == meta["source_sha"] and r["state"] == "CHANGES_REQUESTED"
                   for r in latest.values()):
                return "promotion_pr_changes_requested"
            approved = [r["user"]["login"] for r in latest.values()
                        if r["state"] == "APPROVED" and r.get("commit_id") == meta["source_sha"]]
            if approval == "requester":
                requester = meta["requester"]
                login = requester.get("github_login") if isinstance(requester, dict) else None
                if not login:
                    return "promotion_requester_identity_missing"
                return (None if login.casefold() in {name.casefold() for name in approved}
                        else "promotion_requester_approval_missing")
            if approval == "operator":
                allowed = meta["step"]["gate"].get("operator_logins")
                for login in approved:
                    if allowed is not None and login.casefold() not in {
                        name.casefold() for name in allowed
                    }:
                        continue
                    try:
                        operator = await self.is_operator(login)
                    except GitHubAccessError as exc:
                        if exc.category == "rate_limited":
                            raise
                        return "promotion_operator_permission_unavailable"
                    except (OSError, ValueError, TypeError):
                        return "promotion_operator_permission_unavailable"
                    if operator:
                        return None
                return "promotion_operator_approval_missing"
            return "promotion_intent_invalid"
        except GitHubAccessError as exc:
            if exc.category == "rate_limited":
                raise
            return ("promotion_pr_missing" if exc.category == "not_found_or_hidden"
                    else "promotion_pr_unavailable")
        except (OSError, ValueError, KeyError, TypeError):
            return "promotion_pr_unavailable"


def step_required_checks(trust: IntegrationTrustManifest, step: dict):
    """A step selects the manifest's trust; it cannot invent check names or an App."""
    from src.integration.ci import RequiredChecksManifest

    if step["gate"]["attestation"] not in trust.promotion_attestation_names:
        raise PromotionIntentInvalid("attestation_not_in_manifest")
    selector = step["gate"]["checks"]
    if selector == "inherit":
        names = trust.required_checks.names
    elif selector.startswith("manifest:") and selector[9:] in trust.check_sets:
        names = trust.check_sets[selector[9:]]
    else:
        raise PromotionIntentInvalid("check_set_unknown")
    return RequiredChecksManifest(version=trust.required_checks.version, names=names)


class PromotionChecks:
    """Resolve once outside the fence and reuse exact-SHA source-lane evidence."""

    def __init__(self, admission, resolve, *, pull_request):
        self.admission, self.resolve = admission, resolve
        self.pull_request = pull_request
        self._resolved = {}
        self._heads = {}
        self._pr_reasons = {}

    async def refresh_gate(self, batch, sha):
        from src.integration.batches import BatchStore

        meta = await self.admission.load(batch, await BatchStore(self.admission.db).members(batch.id))
        if sha != meta["source_sha"]:
            raise PromotionIntentInvalid("promotion gate head differs from its intent")
        gate = self.pull_request(batch)
        self._pr_reasons[(batch.id, sha)] = await gate.observe(batch, meta)
        checks = await self.for_candidate(batch, sha)
        await checks.refresh(await self.head(batch, sha))

    def reason(self, batch, sha):
        return self._pr_reasons.get((batch.id, sha), "promotion_pr_unavailable")

    async def head(self, batch, sha):
        from src.integration.batches import BatchStore

        key = (batch.id, sha)
        if key in self._heads:
            return self._heads[key]
        members = await BatchStore(self.admission.db).members(batch.id)
        meta = await self.admission.load(batch, members)
        head = HeadIdentity(repository_id=batch.repository_id,
                            ref=promotion_ref(meta["step"], meta), sha=sha, generation=0)
        self._heads[key] = head
        return head

    async def for_candidate(self, batch, sha):
        key = (batch.id, sha)
        if key not in self._resolved:
            self._resolved[key] = await self.resolve(batch, sha)
            await self.head(batch, sha)
        return self._resolved[key]

    @staticmethod
    def passes(result):
        return result.green

    async def gate(self, batch, sha, tree):
        checks = self._resolved.get((batch.id, sha))
        head = self._heads.get((batch.id, sha))
        return (checks is not None and head is not None and self.reason(batch, sha) is None
                and (await checks.read(head)).green)


async def publish_step_attestation(admission, batch, members, trust, client) -> str:
    """Publish and read back only this admitted step's canonical proof, outside locks."""
    meta = await admission.load(batch, members)
    step = meta["step"]
    required = step_required_checks(trust, step)
    if meta["checks_version"] != required.version:
        raise PromotionIntentInvalid("promotion check version differs from the pinned request")
    selected = trust.model_copy(update={"required_checks": required})
    observer = AuthenticatedGitHubObserver(client, expected_event="push")
    observed = await observer.observe(selected, meta["source_sha"])
    if not isinstance(observed, TrustedCIObservation) or not isinstance(observed.payload, AttestationPayload):
        return "not_green"
    lower = observed.payload
    payload = PromotionAttestationPayload.model_validate({
        "schema": "aq.promotion-attestation.v1",
        "repository": {"canonical_repository_id": trust.canonical_repository_id,
                       "repository_id": trust.repository_id, "full_name": trust.full_name,
                       "ci_producer_app_id": trust.ci_producer_app_id,
                       "attestation_app_id": trust.attestation_app_id},
        "step": step["id"], "target_ref": batch.target_ref,
        "attestation_name": step["gate"]["attestation"], "version": meta.get("version"),
        "request_id": meta["request_id"], "batch_id": batch.id,
        "source_sha": meta["source_sha"], "base_sha": meta["base_sha"],
        "checks_version": required.version,
        "checks": lower.checks, "workflow_runs": lower.workflow_runs,
    })
    if not await admission.eligible(batch, members):
        return "promotion_intent_invalid"
    record_id = await observer.publish(trust, payload)
    from urllib.parse import quote

    records = await client.paged_items(
        f"/repos/{trust.full_name}/commits/{payload.source_sha}/check-runs"
        f"?check_name={quote(payload.attestation_name, safe='')}&filter=all&per_page=100",
        key="check_runs",
    )
    trusted = [r for r in records if r.get("name") == payload.attestation_name
               and type((r.get("app") or {}).get("id")) is int
               and (r.get("app") or {}).get("id") == trust.attestation_app_id]
    if not trusted or any(type(r.get("id")) is not int or r["id"] <= 0 for r in trusted):
        return "unavailable"
    newest = max(trusted, key=lambda r: r["id"])
    return "published" if (
        newest["id"] == record_id and newest.get("status") == "completed"
        and newest.get("conclusion") == "success" and newest.get("head_sha") == payload.source_sha
        and newest.get("external_id") == payload.external_id
        and (newest.get("output") or {}).get("text") == payload.canonical_bytes().decode("ascii")
    ) else "unavailable"


class PromotionVisit(BatchService):
    """Fast-forward S unchanged, with individually recoverable branch and tag writes."""

    def __init__(self, store, gitops, *, admission, checks, publish, publish_tag, delete_ref, attest,
                 snapshot, clock=time.time):
        super().__init__(store, gitops, publish=publish, eligible=admission.eligible,
                         gate=checks.gate, attest=attest)
        self.admission, self.checks = admission, checks
        self.publish_tag, self.snapshot, self.clock = publish_tag, snapshot, clock
        self.delete_ref = delete_ref

    async def cleanup(self, batch, repo, meta, member):
        """Retire only this request's unchanged private ref after Git delivery.

        Cleanup has its own managed-ref lease and read-back. It never gates
        settlement; the batch's pending cleanup state survives a failed delete.
        """
        async def delivered():
            fresh = await self.snapshot()
            return (not fresh.error and fresh.target_oid is not None
                    and (await fresh.contains_source(member.task_id, member.source_sha,
                                                     member.base_sha)) is True
                    and await self.gitops.is_ancestor(repo, member.source_sha, fresh.target_oid))

        try:
            ref = promotion_ref(meta["step"], meta)
            if await self.gitops.remote(repo, ref) == member.source_sha:
                await self.delete_ref(repo, ref, expected_old_oid=member.source_sha,
                                      authorize=delivered)
            if await self.gitops.remote(repo, ref) is None:
                async with self.store.db.immediate() as conn:
                    await conn.execute(update(integration_batches).where(
                        integration_batches.c.id == batch.id,
                    ).values(cleanup_state="complete", updated_at=self.clock()))
        except (GitError, OSError, RuntimeError, ValueError):
            log.warning("promotion %s candidate cleanup deferred", batch.id)

    async def reconcile_cleanup(self, target, *, limit=100):
        """Retry settled requests' private-ref cleanup on later lane visits."""
        from src.integration.batches import Batch

        async with self.store.db._engine.connect() as conn:
            rows = (await conn.execute(select(integration_batches).where(
                integration_batches.c.project_id == target.project_id,
                integration_batches.c.repository_id == target.repository_id,
                integration_batches.c.target_ref == target.target_ref,
                integration_batches.c.trigger == "promotion",
                integration_batches.c.lifecycle == "promoted",
                integration_batches.c.cleanup_state == "pending",
            ).order_by(integration_batches.c.created_at, integration_batches.c.id)
                .limit(limit))).mappings().all()
        for row in rows:
            batch = Batch.from_row(row)
            try:
                members = await self.store.members(batch.id)
                meta = await self.admission.load(batch, members, allow_completed=True)
                repo = await self.gitops.repository(batch)
                await self.gitops.validate_repository(repo)
                member = members[0]
                fresh = await self.snapshot()
                if (fresh.error or not fresh.target_oid
                        or (await fresh.contains_source(member.task_id, member.source_sha,
                                                        member.base_sha)) is not True
                        or not await self.gitops.is_ancestor(repo, member.source_sha,
                                                            fresh.target_oid)):
                    continue
                tag = await self._tag(repo, meta, member)
                state, _oid = await self._tag_state(repo, tag, meta, member)
                if state in {"valid", "none"}:
                    await self.cleanup(batch, repo, meta, member)
            except (GitError, OSError, RuntimeError, ValueError):
                log.warning("promotion %s settled cleanup deferred", batch.id)

    async def _tag(self, repo, meta, member):
        versioning = meta["step"]["versioning"]
        if versioning["kind"] == "none":
            return None
        date = datetime.fromtimestamp(meta.get("requested_at", 0), UTC).strftime("%Y-%m-%d")
        name = versioning["tag_format"].format(
            version=meta.get("version"), sha12=member.source_sha[:12], utc_date=date,
            step=meta["step"]["id"],
        )
        if (versioning["kind"] == "semver_tag"
                and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
                                 meta.get("version") or "") is None):
            raise PromotionIntentInvalid("invalid semver promotion version")
        await self.gitops.run(repo, "check-ref-format", "refs/tags/" + name)
        return name

    async def _tag_state(self, repo, tag, meta, member):
        if tag is None:
            return "none", None
        ref = "refs/tags/" + tag
        observed = await self.gitops.git.als_remote_qualified_refs(
            str(repo.store), [ref, ref + "^{}"],
            repository_url=f"https://github.com/{repo.binding.full_name}.git",
        )
        direct, peeled = observed[ref], observed[ref + "^{}"]
        if direct.state is RemoteRefState.ERROR or peeled.state is RemoteRefState.ERROR:
            return "unknown", None
        if direct.state is RemoteRefState.ABSENT:
            return "absent", None
        if peeled.oid != member.source_sha:
            return "conflict", direct.oid
        result = await self.gitops.git.arun_git_result(
            ["cat-file", "-e", direct.oid], cwd=str(repo.store),
        )
        if result.returncode:
            await self.gitops.git.afetch_repository_tag_oid(
                str(repo.store), repository=repo.binding, oid=direct.oid,
                destination_ref="refs/aq/promotion-tags/" + direct.oid,
            )
        if await self.gitops.run(repo, "cat-file", "-t", direct.oid) != "tag":
            return "conflict", direct.oid
        body = await self.gitops.run(repo, "cat-file", "-p", direct.oid)
        expected = self._tag_message(tag, meta, member)
        headers, _, message = body.partition("\n\n")
        if (message.rstrip() != expected.rstrip()
                or f"object {member.source_sha}" not in headers.splitlines()
                or "type commit" not in headers.splitlines()
                or f"tag {tag}" not in headers.splitlines()):
            return "conflict", direct.oid
        return "valid", direct.oid

    @staticmethod
    def _tag_message(tag, meta, member):
        return (f"Promote {meta['step']['id']} {tag}\n\n"
                f"AQ-Promotion: {member.task_id}@{member.source_sha}\n"
                f"AQ-Promotion-Request: {meta['request_id']}\n")

    async def _create_tag(self, batch, repo, tag, meta, member, authorize):
        # Plumbing avoids a mutable local tag and gives deterministic objects on retry.
        body = (f"object {member.source_sha}\ntype commit\ntag {tag}\n"
                f"tagger aq-integration[bot] <aq-integration[bot]@users.noreply.github.com> "
                f"{int(batch.created_at)} +0000\n\n" + self._tag_message(tag, meta, member))
        result = await self.gitops.git.arun_git_result(
            ["mktag"], cwd=str(repo.store), stdin=body,
        )
        if result.returncode:
            raise GitError(result.stderr or "annotated promotion tag construction failed")
        oid = result.stdout.strip()
        if not is_valid_git_oid(oid):
            raise GitError("annotated promotion tag construction returned no exact object")
        try:
            async with self.store.publication(batch.id) as opened:
                if opened:
                    await self.publish_tag(
                        repo, batch.target_ref, "refs/tags/" + tag, new_oid=oid,
                        expected_old_oid="", authorize=authorize,
                    )
        except (GitError, RuntimeError, ValueError) as exc:
            log.warning("promotion %s tag publication failed: %s", batch.id, exc)
        return await self._tag_state(repo, tag, meta, member)

    async def visit(self, batch, members, snapshot):
        from src.operator_decisions import OperatorDecisions

        members = tuple(members)
        try:
            if await self.store.members(batch.id) != members:
                raise PromotionIntentInvalid("frozen promotion members changed")
            meta = await self.admission.load(batch, members, allow_completed=True)
            member = members[0]
            repo = await self.gitops.repository(batch)
            await self.gitops.validate_repository(repo)
            if (snapshot.observation.project_id, snapshot.observation.repository_id,
                snapshot.observation.target_ref) != (
                batch.project_id, batch.repository_id, batch.target_ref,
            ):
                raise PromotionIntentInvalid("snapshot belongs to another promotion target")
            target, source = snapshot.target_oid, member.source_sha
            if snapshot.error or target is None:
                return BatchObservation("unknown", source, target)
            tag = await self._tag(repo, meta, member)
            tag_state, tag_oid = await self._tag_state(repo, tag, meta, member)
            if tag_state == "conflict":
                return BatchObservation("held", source, target,
                                        detail={"reason": "promotion_tag_conflict"})
            if tag_state == "unknown":
                return BatchObservation("unknown", source, target)
            proof = await snapshot.contains_source(member.task_id, source, member.base_sha)
            if proof is None:
                return BatchObservation("unknown", source, target)
            # Ordinary trains also accept equivalent patches. A promotion must
            # deliver S itself, so an equivalent commit cannot satisfy this lane.
            proof = proof and await self.gitops.is_ancestor(repo, source, target)
            if proof and tag_state in {"valid", "none"}:
                await self.cleanup(batch, repo, meta, member)
                return BatchObservation("delivered", source, target, detail={"tag_oid": tag_oid})
            if (await OperatorDecisions(self.store.db).holds("batch", batch.id)
                    or not await self._authorized(batch, members)):
                return BatchObservation("held", source, target)
            if not proof and not await self.gitops.is_ancestor(repo, target, source):
                return BatchObservation("held", source, target,
                                        detail={"reason": "promotion_not_fast_forward"})
            await self.gitops.exact(repo, source)
            tree = await self.gitops.git.atree_sha(str(repo.store), source)
            if proof:
                # The target write already passed the step gate. Finish only the
                # missing immutable tag and read-back after a crash (§3.3 4b/4c).
                tag_state, tag_oid = await self._create_tag(
                    batch, repo, tag, meta, member,
                    lambda: self._authorized(batch, members),
                )
                fresh = await self.snapshot()
                proof = await fresh.contains_source(member.task_id, source, member.base_sha)
                if tag_state == "conflict":
                    return BatchObservation("held", source, fresh.target_oid, tree,
                                            detail={"reason": "promotion_tag_conflict"})
                if (proof is True and tag_state == "valid" and fresh.target_oid
                        and await self.gitops.is_ancestor(repo, source, fresh.target_oid)):
                    await self.cleanup(batch, repo, meta, member)
                    return BatchObservation("delivered", source, fresh.target_oid, tree,
                                            detail={"tag_oid": tag_oid})
                return BatchObservation("unknown", source, fresh.target_oid, tree)
            # Resolve trust outside the publication fence. A cached lower-lane
            # success needs no candidate push and no new CI request.
            await self.checks.for_candidate(batch, source)
            await self.checks.refresh_gate(batch, source)
            if not await self.gate(batch, source, tree):
                reason = self.checks.reason(batch, source)
                return BatchObservation("held" if reason else "testing", source, target, tree,
                                        detail={"reason": reason or "promotion_checks_pending"})
            if not await self.admission.eligible(batch, members):
                raise PromotionIntentInvalid("promotion admission changed before attestation")
            attestation = await self.attest(batch, source)
            if attestation not in {"published", "already_published"}:
                return BatchObservation("held", source, target, tree,
                                        detail={"reason": "attestation_unavailable"})

            # Reobserve outside the fence after attestation so a review/head
            # changed during its publication cannot authorize the branch write.
            await self.checks.refresh_gate(batch, source)
            if not await self.gate(batch, source, tree):
                return BatchObservation("held", source, target, tree, detail={
                    "reason": self.checks.reason(batch, source) or "promotion_checks_pending",
                })

            async def authorize():
                return await self._authorized(batch, members) and await self.gate(batch, source, tree)

            # Each ref has its own exact lease/read-back; no atomicity is claimed.
            if not proof:
                state = await self._transfer(batch, repo, batch.target_ref, source, target, authorize)
                if state != "published":
                    return BatchObservation(state, source, target, tree)
            if tag_state == "absent":
                tag_state, tag_oid = await self._create_tag(
                    batch, repo, tag, meta, member, authorize,
                )
            fresh = await self.snapshot()
            proof = await fresh.contains_source(member.task_id, source, member.base_sha)
            if tag_state == "conflict":
                return BatchObservation("held", source, fresh.target_oid, tree,
                                        detail={"reason": "promotion_tag_conflict"})
            if (proof is True and tag_state in {"valid", "none"} and fresh.target_oid
                    and await self.gitops.is_ancestor(repo, source, fresh.target_oid)):
                await self.cleanup(batch, repo, meta, member)
                return BatchObservation("delivered", source, fresh.target_oid, tree,
                                        detail={"tag_oid": tag_oid})
            return BatchObservation("unknown", source, fresh.target_oid, tree)
        except PromotionIntentInvalid as exc:
            return BatchObservation("held", detail={"reason": "promotion_intent_invalid",
                                                   "error": str(exc)})
        except (GitError, OSError, ValueError) as exc:
            return BatchObservation("unknown", detail={"reason": str(exc)})


async def settle_promotion(db, batch, observation, *, clock=time.time, conn=None):
    """Called only after the visit's git_truth and optional annotated-tag read-back."""
    if observation.state != "delivered":
        raise ValueError("promotion settlement requires delivery proof")
    if conn is None:
        async with db.immediate() as owned:
            return await settle_promotion(db, batch, observation, clock=clock, conn=owned)
    members = (await conn.execute(select(integration_batch_members.c.task_id).where(
        integration_batch_members.c.batch_id == batch.id,
    ))).scalars().all()
    await conn.execute(update(tasks).where(tasks.c.id.in_(members),
                       tasks.c.task_type == "promotion", tasks.c.status == "IN_PROGRESS")
                       .values(status="COMPLETED", updated_at=clock()))
    result = {"tag_oid": (observation.detail or {}).get("tag_oid"),
              "source_sha": observation.candidate_sha, "batch_id": batch.id}
    for task_id in members:
        await conn.execute(insert(task_metadata).values(
            task_id=task_id, key=PROMOTION_RESULT, value=json.dumps(result, sort_keys=True),
        ).on_conflict_do_nothing(index_elements=["task_id", "key"]))
        recorded = await conn.scalar(select(task_metadata.c.value).where(
            task_metadata.c.task_id == task_id, task_metadata.c.key == PROMOTION_RESULT,
        ))
        if json.loads(recorded) != result:
            raise PromotionIntentInvalid("promotion settlement differs from its recorded result")
