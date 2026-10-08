"""The App-mode trust manifest: one pure builder, its canonical text and a comparison.

``.github/agent-queue-integration.json`` (schema ``aq.integration-trust.v1``,
:class:`src.integration.ci.IntegrationTrustManifest`) is how a repository names
the identities the daemon trusts in App credential mode.  Its callers build
it and must agree byte for byte, so the rules live here once:

* ``aq integration trust-manifest`` (``integration_trust_manifest`` in
  ``src/commands/integration_commands.py``) renders it from the policy, the
  authenticated binding and the daemon's App, and compares it with the
  default-branch copy;
* the App-mode preflight compares a committed copy on the same fields.

Nothing here reads Git, GitHub or the database.  Fields fall in two groups
(App-mode integration train spec §5): the **identity** fields are compared
exactly wherever a manifest is read, and the **check set** is informational in
the tree -- the frozen policy snapshot is the runtime authority -- so a
check-set difference is a warning, never a refusal.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from src.integration.ci import (
    TRUST_MANIFEST_PATH,
    IntegrationTrustManifest,
    is_numeric_producer_id,
)

SCHEMA = "aq.integration-trust.v1"
ATTESTATION_NAME = "Agent Queue Integration Attestation"

#: Compared exactly at every read (spec §5.1).  ``schema`` is listed so a
#: manifest of another schema is an identity difference, not a pass.
IDENTITY_FIELDS = (
    "schema",
    "canonical_repository_id",
    "repository_id",
    "full_name",
    "ci_producer_app_id",
    "attestation_app_id",
    "attestation_name",
    "promotion_attestation_names",
)
#: Informational in the tree (spec §5.2); the snapshot owns the check set.
CHECK_SET_FIELDS = ("required_checks.version", "required_checks.names", "check_sets")
FIELDS = IDENTITY_FIELDS + CHECK_SET_FIELDS

#: The largest committed copy compared; the attestation service reads no larger.
MAX_MANIFEST_BYTES = 64 * 1024

_ABSENT = object()


class TrustManifestRefusal(ValueError):
    """A manifest cannot be built; ``code`` is the blocker the preflight uses."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def build_trust_manifest(
    *,
    canonical_repository_id: str,
    repository_id: int,
    full_name: str,
    ci_producer_app_id: int | None,
    attestation_app_id: int,
    checks: Sequence[str],
    check_version: str,
    promotion_attestation_names: Sequence[str] | None = None,
    check_sets: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The manifest object, validated by :class:`IntegrationTrustManifest`.

    ``checks`` keeps its order: the runtime compares check names as an
    ordered sequence, so the manifest lists them exactly as the policy does.

    ``ci_producer_app_id=None`` is a comparison template for a policy whose
    producer is itself at fault (``ci_producer_not_numeric``): the field is
    ``None``, nothing is validated, and :func:`compare` is told to ignore it.
    It is never a manifest to commit.
    """
    manifest = {
        "schema": SCHEMA,
        "canonical_repository_id": canonical_repository_id,
        "repository_id": repository_id,
        "full_name": full_name,
        "ci_producer_app_id": ci_producer_app_id,
        "attestation_app_id": attestation_app_id,
        "attestation_name": ATTESTATION_NAME,
        "required_checks": {"version": check_version, "names": list(checks)},
    }
    if promotion_attestation_names:
        manifest["promotion_attestation_names"] = list(promotion_attestation_names)
    if check_sets:
        manifest["check_sets"] = {name: list(values) for name, values in check_sets.items()}
    if ci_producer_app_id is not None:
        IntegrationTrustManifest.model_validate(manifest)
    return manifest


def _required_checks(policy: Any, boundary: str) -> Mapping[str, Any]:
    if isinstance(policy, BaseModel):
        policy = policy.model_dump(mode="json")
    selected = policy.get(boundary) if isinstance(policy, Mapping) else None
    required = selected.get("required_checks") if isinstance(selected, Mapping) else None
    if not isinstance(required, Mapping):
        raise TrustManifestRefusal(
            "ci_policy_invalid", f"the policy's {boundary} boundary has no required checks"
        )
    return required


def policy_producer_app_id(policy: Any) -> int:
    """The numeric CI producer both policy boundaries name.

    App credential mode compares the producer with the manifest's numeric
    ``ci_producer_app_id``, so a slug is ``ci_producer_not_numeric`` (spec §4)
    and two different numeric producers are ``ci_producer_mismatch``: one
    manifest cannot name both.
    """
    producers = {
        boundary: _required_checks(policy, boundary).get("producer_id")
        for boundary in ("parent", "root")
    }
    slugs = [
        f"{boundary}={producer!r}"
        for boundary, producer in producers.items()
        if not is_numeric_producer_id(producer)
    ]
    if slugs:
        raise TrustManifestRefusal(
            "ci_producer_not_numeric",
            "App credential mode needs the CI producer's numeric App id on both boundaries "
            f"(GitHub Actions is \"15368\"); the policy names {', '.join(slugs)}",
        )
    if producers["parent"] != producers["root"]:
        raise TrustManifestRefusal(
            "ci_producer_mismatch",
            f"the parent ({producers['parent']}) and root ({producers['root']}) boundaries "
            "name different CI producers; one trust manifest names one producer",
        )
    return int(producers["root"])


def manifest_for_policy(
    policy: Any,
    *,
    canonical_repository_id: str,
    repository_id: int,
    full_name: str,
    attestation_app_id: int,
    promotion_flow: Sequence[Mapping[str, Any]] = (),
    check_sets: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """The manifest a bound (or candidate) policy implies for one repository.

    The producer comes from the policy, the check set from its ``root``
    boundary: only root candidates are attested (spec §6.1, §7.1).
    """
    producer = policy_producer_app_id(policy)
    required = _required_checks(policy, "root")
    try:
        return build_trust_manifest(
            canonical_repository_id=canonical_repository_id,
            repository_id=repository_id,
            full_name=full_name,
            ci_producer_app_id=producer,
            attestation_app_id=attestation_app_id,
            checks=list(required.get("names") or ()),
            check_version=required.get("version"),
            promotion_attestation_names=flow_attestation_names(promotion_flow),
            check_sets=check_sets,
        )
    except ValidationError as exc:
        # The identities come from the binding and the App: a failure here is
        # the policy's (an empty check set) or an App that is its own producer.
        raise TrustManifestRefusal(
            "trust_manifest_invalid", f"the manifest would not validate: {exc}"
        ) from exc


def flow_attestation_names(flow: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """The bound flow's trust identities, in chain order, without consulting Git."""
    if not isinstance(flow, Sequence) or isinstance(flow, (str, bytes)):
        raise TrustManifestRefusal("promotion_flow_invalid", "the stored promotion flow is invalid")
    names = []
    for step in flow:
        gate = step.get("gate") if isinstance(step, Mapping) else None
        name = gate.get("attestation") if isinstance(gate, Mapping) else None
        if not isinstance(name, str) or not name:
            raise TrustManifestRefusal(
                "promotion_flow_invalid", "a stored promotion step has no attestation identity"
            )
        names.append(name)
    return tuple(dict.fromkeys(names))


def canonical_text(manifest: Mapping[str, Any]) -> str:
    """The committed form: sorted keys, two-space indent, one trailing newline.

    Key order matches ``.github/agent-queue-integration.example.json``; list
    order is preserved, because the check-name comparison is ordered.
    """
    return json.dumps(manifest, indent=2, sort_keys=True) + "\n"


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _field(obj: Mapping[str, Any], dotted: str) -> Any:
    value: Any = obj
    for part in dotted.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return _ABSENT
        value = value[part]
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate field {key!r}")
        value[key] = item
    return value


def _unexpected_fields(committed: Mapping[str, Any]) -> list[str]:
    top = {field.split(".", 1)[0] for field in FIELDS}
    unexpected = [key for key in committed if key not in top]
    required = committed.get("required_checks")
    if isinstance(required, Mapping):
        nested = {field.split(".", 1)[1] for field in CHECK_SET_FIELDS if "." in field}
        unexpected += [f"required_checks.{key}" for key in required if key not in nested]
    return unexpected


@dataclass(frozen=True)
class FieldDiff:
    field: str
    #: ``identity``, ``check_set`` or ``unexpected`` (a field the schema forbids).
    kind: str
    expected: Any
    committed: Any
    #: False when the committed copy lacks the field.
    committed_present: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "kind": self.kind,
            "expected": self.expected,
            "committed": self.committed,
            "committed_present": self.committed_present,
        }


@dataclass(frozen=True)
class ManifestComparison:
    """How a committed copy relates to the expected manifest.

    ``identity_equal`` and ``check_set_equal`` also require the copy to
    validate: a manifest the daemon cannot parse trusts nothing.
    """

    present: bool
    valid: bool
    identity_equal: bool
    check_set_equal: bool
    #: The copy is byte-identical to :func:`canonical_text` of itself.
    canonical: bool
    diff: tuple[FieldDiff, ...] = ()
    error: str | None = None

    @property
    def status(self) -> str:
        """``ok``, ``warn`` (check set or formatting only) or ``fail``."""
        if not self.identity_equal:
            return "fail"
        return "ok" if self.check_set_equal and self.canonical else "warn"

    @property
    def code(self) -> str | None:
        """The failure code, in the preflight's vocabulary."""
        if not self.present or not self.valid:
            return "trust_manifest_unavailable"
        if not self.identity_equal:
            return "trust_manifest_mismatch"
        return None

    @property
    def warnings(self) -> tuple[str, ...]:
        if not self.identity_equal:
            return ()
        codes = []
        if not self.check_set_equal:
            codes.append("trust_manifest_check_set_differs")
        if not self.canonical:
            codes.append("trust_manifest_noncanonical")
        return tuple(codes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "valid": self.valid,
            "identity_equal": self.identity_equal,
            "check_set_equal": self.check_set_equal,
            "canonical": self.canonical,
            "status": self.status,
            "code": self.code,
            "warnings": list(self.warnings),
            "diff": [item.as_dict() for item in self.diff],
            "error": self.error,
        }


def _absent(error: str | None = None) -> ManifestComparison:
    return ManifestComparison(
        present=False, valid=False, identity_equal=False, check_set_equal=False,
        canonical=False, error=error,
    )


def compare(
    expected: Mapping[str, Any],
    committed: bytes | str | None,
    *,
    ignore: Collection[str] = (),
) -> ManifestComparison:
    """Compare a committed copy (raw file content, ``None`` when absent) field by field.

    Fields in ``ignore`` are left out of the diff; the copy must still validate.
    """
    if committed is None:
        return _absent()
    raw = committed.encode("utf-8") if isinstance(committed, str) else committed
    if len(raw) > MAX_MANIFEST_BYTES:
        return ManifestComparison(
            present=True, valid=False, identity_equal=False, check_set_equal=False,
            canonical=False, error=f"larger than {MAX_MANIFEST_BYTES} bytes",
        )
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        return ManifestComparison(
            present=True, valid=False, identity_equal=False, check_set_equal=False,
            canonical=False, error=f"not a JSON document: {exc}",
        )
    if not isinstance(parsed, dict):
        return ManifestComparison(
            present=True, valid=False, identity_equal=False, check_set_equal=False,
            canonical=False, error="not a JSON object",
        )

    error = None
    try:
        IntegrationTrustManifest.model_validate(parsed)
        valid = True
    except ValidationError as exc:
        valid = False
        error = f"does not validate as {SCHEMA}: {exc.error_count()} error(s)"

    diff: list[FieldDiff] = []
    for name in FIELDS:
        if name in ignore:
            continue
        want, have = _field(expected, name), _field(parsed, name)
        present = have is not _ABSENT
        # Existing manifests predate promotion flows. Omitting an optional
        # empty collection means the same trust as its schema default.
        if name in {"promotion_attestation_names", "check_sets"}:
            default = [] if name == "promotion_attestation_names" else {}
            want = default if want is _ABSENT else want
            have = default if have is _ABSENT else have
        # ``type`` too: the schema's integers are strict, so "15368" != 15368.
        if have is _ABSENT or type(want) is not type(have) or want != have:
            diff.append(
                FieldDiff(
                    field=name,
                    kind="identity" if name in IDENTITY_FIELDS else "check_set",
                    expected=want,
                    committed=None if have is _ABSENT else have,
                    committed_present=present,
                )
            )
    for name in _unexpected_fields(parsed):
        diff.append(
            FieldDiff(
                field=name, kind="unexpected", expected=None, committed=_field(parsed, name),
            )
        )
    kinds = {item.kind for item in diff}
    return ManifestComparison(
        present=True,
        valid=valid,
        identity_equal=valid and not kinds & {"identity", "unexpected"},
        check_set_equal=valid and "check_set" not in kinds,
        canonical=text == canonical_text(parsed),
        diff=tuple(diff),
        error=error,
    )


__all__ = [
    "ATTESTATION_NAME",
    "CHECK_SET_FIELDS",
    "FIELDS",
    "IDENTITY_FIELDS",
    "MAX_MANIFEST_BYTES",
    "SCHEMA",
    "TRUST_MANIFEST_PATH",
    "FieldDiff",
    "ManifestComparison",
    "TrustManifestRefusal",
    "build_trust_manifest",
    "canonical_text",
    "compare",
    "flow_attestation_names",
    "manifest_for_policy",
    "policy_producer_app_id",
    "text_sha256",
]
