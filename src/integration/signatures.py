"""Versioned train evidence; no selection, worker-test policy or database writes.

Producers must supply trusted, exact-head classification/provenance observations.
A digest detects changed evidence, not authority. Graph observations never relax
path conflicts in v1. CommandHandler consumers freeze these documents only after
rechecking ordinary admission and every SignatureIdentity immediately before use.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass

from src.git.manager import is_valid_git_oid

SIGNATURE_VERSION = 1
SIGNATURE_CACHE_SECONDS = 600.0
SIGNATURE_CACHE_LIMIT = 2048
SIGNATURE_DOCUMENT_KEY = "train_signatures"


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()


def _path(value: str) -> bool:
    return isinstance(value, str) and bool(value) and "\0" not in value and not (
        value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/"))
    )


def _sha256(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


@dataclass(frozen=True)
class SignatureIdentity:
    project_id: str
    repository_id: str
    task_id: str
    pr_id: str | None
    source_ref: str
    target_ref: str
    merge_base: str
    source_head: str
    target_sha: str
    policy_revision: str
    generator_digest: str
    graph_digest: str | None = None
    suite: str | None = None

    @property
    def context(self) -> tuple:
        return (self.project_id, self.repository_id, self.target_ref, self.target_sha,
                self.policy_revision, self.generator_digest, self.graph_digest, self.suite)

    @property
    def valid(self) -> bool:
        return (
            all(isinstance(value, str) and bool(value.strip()) for value in (
                self.project_id, self.repository_id, self.task_id, self.policy_revision,
            ))
            and (self.pr_id is None or isinstance(self.pr_id, str) and bool(self.pr_id))
            and all(isinstance(ref, str) and ref.startswith("refs/heads/")
                    and len(ref) > len("refs/heads/") for ref in (self.source_ref, self.target_ref))
            and all(is_valid_git_oid(oid) for oid in (
                self.merge_base, self.source_head, self.target_sha,
            ))
            and _sha256(self.generator_digest)
            and (self.graph_digest is None or _sha256(self.graph_digest))
            and (self.suite is None or isinstance(self.suite, str) and bool(self.suite))
        )


@dataclass(frozen=True)
class PathChange:
    """A name-status diff record. R/C retain both names; D retains its old name."""

    status: str
    path: str
    old_path: str | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        return (self.path,) if self.old_path is None else (self.old_path, self.path)

    @property
    def valid(self) -> bool:
        return (
            self.status in {"A", "M", "D", "T", "R", "C"}
            and (self.old_path is not None) == (self.status in {"R", "C"})
            and all(_path(path) for path in self.paths)
        )


@dataclass(frozen=True)
class GeneratedOutput:
    path: str
    inputs: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "inputs", tuple(self.inputs))


@dataclass(frozen=True)
class SignatureRules:
    """Exact path registry, including deleted/renamed paths from the source base.

    Known paths are explicitly classified source inputs, not merely all tracked
    files. Global policy/toolchain paths belong in universal_paths. Output/input
    mappings must come from the pinned generator registry, never Git attributes.
    """

    known_paths: tuple[str, ...]
    revision: str
    generated: tuple[GeneratedOutput, ...] = ()
    universal_paths: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("known_paths", "generated", "universal_paths"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def digest(self) -> str:
        return _digest({
            "revision": self.revision,
            "known_paths": sorted(set(self.known_paths)),
            "generated": sorted((g.path, sorted(set(g.inputs))) for g in self.generated),
            "universal_paths": sorted(set(self.universal_paths)),
        })

    @property
    def valid(self) -> bool:
        outputs = {g.path for g in self.generated}
        return (
            isinstance(self.revision, str) and bool(self.revision)
            and len(outputs) == len(self.generated)
            and all(_path(p) for p in (*self.known_paths, *self.universal_paths, *outputs))
            and all(g.inputs and all(_path(p) and p not in outputs and p in self.known_paths
                                     for p in g.inputs) for g in self.generated)
        )


@dataclass(frozen=True)
class GeneratedProvenance:
    """Result of a trusted provenance check, bound to this full identity.

    Only successfully reproduced outputs are listed. The receipt digest names
    the producer's retained evidence; this object does not run a generator or
    accept an untrusted task's claim that it ran one.
    """

    identity: SignatureIdentity
    outputs: tuple[str, ...]
    receipt_digest: str

    def __post_init__(self):
        object.__setattr__(self, "outputs", tuple(self.outputs))


@dataclass(frozen=True)
class GraphImpact:
    path: str
    owning_targets: tuple[str, ...]
    reverse_test_targets: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "owning_targets", tuple(self.owning_targets))
        object.__setattr__(self, "reverse_test_targets", tuple(self.reverse_test_targets))


@dataclass(frozen=True)
class GraphObservation:
    """Exact-head owning + reverse-test query results, observational until cutover."""

    identity: SignatureIdentity
    impacts: tuple[GraphImpact, ...]

    def __post_init__(self):
        object.__setattr__(self, "impacts", tuple(self.impacts))


@dataclass(frozen=True)
class SourceSignature:
    identity: SignatureIdentity
    paths: tuple[str, ...] = ()
    excluded_outputs: tuple[str, ...] = ()
    owning_targets: tuple[str, ...] = ()
    reverse_test_targets: tuple[str, ...] = ()
    uncovered_paths: tuple[str, ...] = ()
    graph_state: str = "unknown"
    universal: bool = True
    reasons: tuple[str, ...] = ("unknown",)
    provenance_digest: str | None = None
    signature_version: int = SIGNATURE_VERSION

    def __post_init__(self):
        for name in ("paths", "excluded_outputs", "owning_targets", "reverse_test_targets",
                     "uncovered_paths", "reasons"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def valid(self) -> bool:
        groups = (self.paths, self.excluded_outputs, self.owning_targets,
                  self.reverse_test_targets, self.uncovered_paths, self.reasons)
        return (
            type(self.signature_version) is int and self.signature_version == SIGNATURE_VERSION
            and self.identity.valid and type(self.universal) is bool
            and all(all(isinstance(p, str) and p for p in group)
                    and tuple(sorted(set(group))) == group for group in groups)
            and all(_path(p) for p in (*self.paths, *self.excluded_outputs))
            and not set(self.paths).intersection(self.excluded_outputs)
            and set(self.uncovered_paths).issubset(self.paths)
            and bool(self.reasons) and isinstance(self.graph_state, str)
            and self.graph_state in {"unknown", "observational"}
            and (self.graph_state != "unknown" or not (
                self.owning_targets or self.reverse_test_targets
            ))
            and (self.graph_state != "observational" or bool(
                self.identity.graph_digest and self.identity.suite
            ))
            and (self.provenance_digest is None or _sha256(self.provenance_digest))
            and (not self.excluded_outputs or self.provenance_digest is not None)
        )

    @property
    def digest(self) -> str:
        return _digest(asdict(self))

    def to_document(self) -> dict:
        # JSON round-trip also ensures fresh lists and no shared mutable fields.
        return json.loads(json.dumps({**asdict(self), "digest": self.digest}))

    def conflicts_with(self, other: SourceSignature) -> bool:
        """Unknown/mixed policy evidence conflicts. Graph data cannot subtract paths."""
        return (
            not self.valid or not other.valid or self.identity.context != other.identity.context
            or self.universal or other.universal
            or bool(set(self.paths).intersection(other.paths))
        )


def build_source_signature(
    identity: SignatureIdentity,
    changes: Sequence[PathChange] | None,
    rules: SignatureRules,
    *,
    provenance: GeneratedProvenance | None = None,
    graph: GraphObservation | None = None,
) -> SourceSignature:
    """Pure conservative signature of the source merge-base → head diff.

    None means unavailable, unlike an observed empty diff. Unknown paths,
    unproven generated outputs and generated-only edits are universal conflicts.
    """
    if not identity.valid:
        return SourceSignature(identity, reasons=("invalid_identity",))
    if not rules.valid or rules.digest != identity.generator_digest:
        return SourceSignature(identity, reasons=("registry_mismatch",))
    if changes is None or any(not change.valid for change in changes):
        return SourceSignature(identity, reasons=("unknown_diff",))
    paths = {path for change in changes for path in change.paths}
    outputs = {g.path: set(g.inputs) for g in rules.generated}
    reasons = set()
    if paths.intersection(rules.universal_paths):
        reasons.add("global_path")
    if paths - set(rules.known_paths) - outputs.keys() - set(rules.universal_paths):
        reasons.add("unknown_path")
    excluded = set()
    valid_provenance = (
        provenance is not None and provenance.identity == identity
        and _sha256(provenance.receipt_digest)
    )
    for path in paths & outputs.keys():
        if not outputs[path].intersection(paths - outputs.keys()):
            reasons.add("generated_output_only")
        elif not valid_provenance or path not in provenance.outputs:
            reasons.add("generated_provenance_unavailable")
        else:
            excluded.add(path)
    retained = paths - excluded
    owners, tests, covered = set(), set(), set()
    graph_state = "unknown"
    if graph is not None and graph.identity == identity and identity.graph_digest and identity.suite:
        impacts = {impact.path: impact for impact in graph.impacts}
        if len(impacts) == len(graph.impacts) and all(
            _path(i.path) and i.owning_targets
            and all(isinstance(t, str) and t.startswith(("//", "@")) and ":" in t
                    for t in (*i.owning_targets, *i.reverse_test_targets))
            for i in graph.impacts
        ):
            graph_state = "observational"
            for path in retained & impacts.keys():
                covered.add(path)
                owners.update(impacts[path].owning_targets)
                tests.update(impacts[path].reverse_test_targets)
    return SourceSignature(
        identity, tuple(sorted(retained)), tuple(sorted(excluded)), tuple(sorted(owners)),
        tuple(sorted(tests)), tuple(sorted(retained - covered)), graph_state,
        bool(reasons), tuple(sorted(reasons)) or ("path_signature",),
        provenance.receipt_digest if valid_provenance and excluded else None,
    )


class SignatureCache:
    """LRU with absolute TTL; hits never extend ten-minute evidence retention.

    The Git observer rechecks identities even on hits. Evidence keys additionally
    bind classification/provenance/graph inputs so a failed producer cannot reuse
    an earlier successful result for the same head.
    """

    def __init__(self, *, limit=SIGNATURE_CACHE_LIMIT, ttl=SIGNATURE_CACHE_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        if not 1 <= limit <= SIGNATURE_CACHE_LIMIT or not 0 < ttl <= SIGNATURE_CACHE_SECONDS:
            raise ValueError("invalid signature cache bounds")
        self.limit, self.ttl, self.clock = limit, ttl, clock
        self._entries: OrderedDict[tuple, tuple[float, SourceSignature]] = OrderedDict()

    def get(self, key: tuple) -> SourceSignature | None:
        now = self.clock()
        for expired in [k for k, (deadline, _) in self._entries.items() if deadline <= now]:
            del self._entries[expired]
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[1]

    def put(self, key: tuple, value: SourceSignature) -> None:
        self._entries[key] = (self.clock() + self.ttl, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.limit:
            self._entries.popitem(last=False)

    def invalidate(self, identity: SignatureIdentity) -> None:
        for key in [k for k, (_, sig) in self._entries.items() if sig.identity == identity]:
            del self._entries[key]


def freeze_signature_documents(
    artifact_snapshot: Mapping,
    source_manifest: Mapping,
    signatures: Sequence[SourceSignature],
    *,
    current_identities: Sequence[SignatureIdentity],
    selection_reasons: Sequence[str],
) -> tuple[dict, dict]:
    """Return copies for the existing JSON columns; persist via CommandHandler.

    The caller supplies identities freshly rechecked under its admission fence.
    Existing frozen evidence may be replayed identically, never overwritten.
    No selection permission or successful validation is encoded by this helper.
    """
    if (not signatures or tuple(s.identity for s in signatures) != tuple(current_identities)
            or any(not s.valid for s in signatures)
            or len({s.identity.context for s in signatures}) != 1
            or len({s.identity.task_id for s in signatures}) != len(signatures)
            or not selection_reasons or not all(isinstance(r, str) and r for r in selection_reasons)):
        raise ValueError("signature freeze requires current, versioned inputs and reasons")
    envelope = {"signature_version": SIGNATURE_VERSION,
                "sources": [s.to_document() for s in signatures],
                "selection_reasons": list(selection_reasons)}
    envelope["digest"] = _digest(envelope)
    result = []
    for document in (artifact_snapshot, source_manifest):
        if SIGNATURE_DOCUMENT_KEY in document and document[SIGNATURE_DOCUMENT_KEY] != envelope:
            raise ValueError("cannot replace frozen signature evidence")
        result.append({**deepcopy(dict(document)), SIGNATURE_DOCUMENT_KEY: deepcopy(envelope)})
    return result[0], result[1]


def read_signature_documents(
    artifact_snapshot: Mapping | None,
    source_manifest: Mapping | None,
    *,
    current_identities: Sequence[SignatureIdentity],
) -> tuple[SourceSignature, ...] | None:
    """None means legacy/full-check fallback: absent, stale, mixed or corrupt evidence.

    Compare JSON only in Python, never SQL equality/grouping over artifact_snapshot.
    A successful read is evidence, never an authorization or a validation verdict.
    """
    try:
        left = artifact_snapshot[SIGNATURE_DOCUMENT_KEY]
        right = source_manifest[SIGNATURE_DOCUMENT_KEY]
        if left != right or type(left["signature_version"]) is not int or (
            left["signature_version"] != SIGNATURE_VERSION
        ):
            return None
        if left["digest"] != _digest({k: v for k, v in left.items() if k != "digest"}):
            return None
        signatures = []
        for row in left["sources"]:
            data = {k: v for k, v in row.items() if k != "digest"}
            data["identity"] = SignatureIdentity(**data["identity"])
            for key in ("paths", "excluded_outputs", "owning_targets", "reverse_test_targets",
                        "uncovered_paths", "reasons"):
                data[key] = tuple(data[key])
            signature = SourceSignature(**data)
            if (type(signature.signature_version) is not int
                    or signature.signature_version != SIGNATURE_VERSION
                    or signature.to_document() != row):
                return None
            signatures.append(signature)
        # Reuse freeze validation for duplicate members and current identity checks.
        frozen, _ = freeze_signature_documents(
            {}, {}, signatures, current_identities=current_identities,
            selection_reasons=left["selection_reasons"],
        )
        return tuple(signatures) if frozen[SIGNATURE_DOCUMENT_KEY] == left else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
