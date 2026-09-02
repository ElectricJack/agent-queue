"""Explicit V2 activation health, intentionally independent of enablement.

Two halves:

* :func:`evaluate_health` — the pure decision function of child plan §11.  It
  compares what an artifact was compiled against with what the daemon runs
  today, for **both** kinds of dependency the design spec names: command
  execution contracts and capability profiles ("a referenced execution
  contract or capability profile changed" — design spec, *Compatibility,
  rebuild, and failure behavior*).
* :func:`load_activation_health` — the read path that feeds it.  Health is
  computed from stored rows plus the live registries; nothing here executes,
  compiles or repairs anything, and nothing here writes.

Profile fingerprints are deliberately **not** part of
``definition.contract_fingerprint`` (see its docstring): a capability change
is an activation-health question, and this module is where that question is
asked.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from src.playbooks.artifact_ref import SHA256_RE, ArtifactRef
from src.playbooks.run_state import ArtifactVerificationFailed

logger = logging.getLogger(__name__)


class ActivationHealth(str, Enum):
    READY = "ready"
    QUESTION_REQUIRED = "question_required"
    INVALID = "invalid"
    DISABLED = "disabled"
    STALE_CONTRACT = "stale_contract"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class HealthReason:
    code: str
    message: str
    subject: str | None = None
    expected_fingerprint: str | None = None
    actual_fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The ``ActivationHealthReasonDTO`` shape, field for field."""
        return {
            "code": self.code,
            "message": self.message,
            "subject": self.subject,
            "expected_fingerprint": self.expected_fingerprint,
            "actual_fingerprint": self.actual_fingerprint,
        }


def profile_fingerprint(profiles: Mapping[str, str]) -> str:
    """The one aggregate over an artifact's per-profile capability fingerprints.

    This is what ``playbook_artifacts.profile_fingerprint`` holds and what
    ``ArtifactStore.put``'s ``profile_fingerprint`` keyword is given: a single
    opaque digest over ``compiled_against.profiles``, computed the same way
    ``definition.contract_fingerprint`` covers ``compiled_against.commands`` so
    the two provenance columns have one derivation rule between them.

    Defined here rather than in ``definition.py`` because it is not part of
    artifact identity — the artifact hash already covers the mapping; this is
    the row-level value the health read path compares against the registry.
    """
    payload = json.dumps(
        {str(key): str(value) for key, value in sorted(profiles.items())},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def evaluate_health(
    *,
    enabled: bool,
    artifact: ArtifactRef | None,
    artifact_present: bool,
    validation: Mapping[str, Any],
    current_contract_fingerprints: Mapping[str, str],
    artifact_contract_fingerprints: Mapping[str, str],
    current_profile_fingerprints: Mapping[str, str] | None = None,
    artifact_profile_fingerprints: Mapping[str, str] | None = None,
    stored_profile_fingerprint: str | None = None,
    artifact_sha_mismatch: bool = False,
) -> tuple[ActivationHealth, tuple[HealthReason, ...]]:
    """Health for one activation.  First match wins; the order is the contract.

    ``*_profile_fingerprints`` mirror the command arguments exactly:
    ``artifact_profile_fingerprints`` is the artifact's own
    ``compiled_against.profiles``, and ``current_profile_fingerprints`` is
    built by the caller as ``{profile_id: policy.fingerprint()}`` with an
    unregistered profile **omitted** so it reads as removed.

    ``stored_profile_fingerprint`` is the aggregate the artifact row recorded
    at compile time (:func:`profile_fingerprint`).  It is compared with the
    aggregate of the live per-profile values, and only when it is a
    well-formed digest: the column defaults to ``""`` for artifacts written
    before it was populated, and an opaque non-digest value belongs to a
    caller with its own convention.  Neither may be read as a mismatch — a
    false ``stale_contract`` blocks runs.
    """
    if artifact_sha_mismatch:
        return ActivationHealth.UNAVAILABLE, (
            HealthReason(
                "artifact_sha_mismatch",
                "Artifact bytes do not match the active artifact SHA-256",
                expected_fingerprint=artifact.artifact_sha256 if artifact else None,
            ),
        )
    if artifact is None or not artifact_present:
        return ActivationHealth.UNAVAILABLE, (HealthReason("artifact_missing", "Artifact is unavailable"),)
    errors = validation.get("errors", [])
    if errors:
        return ActivationHealth.INVALID, tuple(
            HealthReason("validation_failed", str(error), getattr(error, "step_id", None))
            for error in errors
        )
    questions = validation.get("questions", [])
    if questions:
        return ActivationHealth.QUESTION_REQUIRED, tuple(
            HealthReason("question_required", str(question), getattr(question, "step_id", None))
            for question in questions
        )
    stale: list[HealthReason] = []
    for command, expected in artifact_contract_fingerprints.items():
        actual = current_contract_fingerprints.get(command)
        if actual is None:
            stale.append(HealthReason("command_removed", "Command is no longer registered", command, expected, None))
        elif actual != expected:
            stale.append(HealthReason("command_contract_changed", "Command contract changed", command, expected, actual))
    current_profiles = current_profile_fingerprints or {}
    artifact_profiles = artifact_profile_fingerprints or {}
    for profile_id, expected in artifact_profiles.items():
        actual = current_profiles.get(profile_id)
        if actual is None:
            stale.append(
                HealthReason(
                    "profile_removed", "Capability profile is no longer registered", profile_id, expected, None
                )
            )
        elif actual != expected:
            stale.append(
                HealthReason(
                    "profile_capabilities_changed",
                    "Capability profile changed",
                    profile_id,
                    expected,
                    actual,
                )
            )
    if not stale and stored_profile_fingerprint and SHA256_RE.fullmatch(stored_profile_fingerprint):
        # The per-profile comparison above is strictly more informative, so
        # this only ever fires when the artifact's own map cannot explain the
        # difference -- an artifact recorded against a profile set the row
        # aggregate does not agree with.
        current_aggregate = profile_fingerprint(current_profiles)
        if current_aggregate != stored_profile_fingerprint:
            stale.append(
                HealthReason(
                    "profile_fingerprint_changed",
                    "Capability profile fingerprint changed",
                    None,
                    stored_profile_fingerprint,
                    current_aggregate,
                )
            )
    if stale:
        return ActivationHealth.STALE_CONTRACT, tuple(stale)
    if not enabled:
        return ActivationHealth.DISABLED, ()
    return ActivationHealth.READY, ()


# ---------------------------------------------------------------------------
# Read path — stored rows + live registries -> health
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActivationHealthRecord:
    """One activation row with the health computed for it right now.

    Field names are the ``ActivationStateDTO`` wire names, so Package 5's
    projection is a copy rather than a rename.
    """

    activation_id: str
    playbook_id: str
    scope: str
    scope_identifier: str
    enabled: bool
    active_artifact_sha256: str | None
    health: ActivationHealth
    reasons: tuple[HealthReason, ...]
    activated_at: float | None = None
    activated_by: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "scope": self.scope,
            "scope_identifier": self.scope_identifier,
            "enabled": self.enabled,
            "active_artifact_sha256": self.active_artifact_sha256,
            "health": self.health.value,
            "reasons": [reason.as_dict() for reason in self.reasons],
            "activated_at": self.activated_at,
            "activated_by": self.activated_by,
        }


def _artifact_snapshots(definition: Any) -> tuple[dict[str, str], dict[str, str]]:
    """``(commands, profiles)`` from an artifact's ``compiled_against``."""
    compiled_against = getattr(definition, "compiled_against", None)
    if compiled_against is None:
        return {}, {}
    return (
        dict(getattr(compiled_against, "commands", {}) or {}),
        dict(getattr(compiled_against, "profiles", {}) or {}),
    )


def _load_definition(path: str | None, artifact_sha256: str | None) -> Any | None:
    """The stored artifact, or ``None`` when it is gone or unreadable.

    Unreadable counts as absent on purpose: ``playbooks.artifact_integrity``
    is the check that names a missing file, and health should say
    ``unavailable`` rather than invent a validation error it cannot describe.
    A readable file whose bytes do not match the activation's SHA is different:
    it raises :class:`ArtifactVerificationFailed`, just like
    :meth:`ArtifactStore.load`, so callers cannot accidentally trust mutable
    content at the path stored in the artifact row.
    """
    if not path:
        return None
    from src.playbooks.definition import PlaybookDefinition

    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError:
        return None
    actual_sha256 = "sha256:" + hashlib.sha256(data).hexdigest()
    if artifact_sha256 and actual_sha256 != artifact_sha256:
        raise ArtifactVerificationFailed(f"artifact at {target} does not match {artifact_sha256}")
    try:
        return PlaybookDefinition.model_validate_json(data)
    except Exception:  # noqa: BLE001 - any malformed artifact reads as absent
        logger.warning("Playbook V2 artifact at %s could not be loaded for health", path)
        return None


def _validation_record(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, Mapping) else {}


async def load_activation_health(
    db: Any,
    *,
    contracts: Any,
    profiles: Any,
    enabled_only: bool = False,
) -> list[ActivationHealthRecord]:
    """Health for every activation, computed from storage and the registries.

    ``contracts`` and ``profiles`` are the ``ContractLookup`` / ``ProfileLookup``
    seams of ``src/playbooks/validation.py`` — passed in rather than imported
    so this function stays testable without a vault and so the daemon has one
    construction site for both (``CommandHandler._v2_lookups``).

    Read-only.  Persisting what it returns is the caller's decision; the
    retention sweep's one-directional downgrade (§12.2) stays the only writer.
    """
    records: list[ActivationHealthRecord] = []
    for row in await db.list_playbook_activations(enabled_only=enabled_only):
        sha = row.get("active_artifact_sha256")
        artifact_row = await db.get_playbook_artifact_row(sha) if sha else None
        ref = ArtifactRef.from_row(artifact_row) if artifact_row else None
        artifact_sha_mismatch = False
        try:
            definition = _load_definition(
                artifact_row.get("path") if artifact_row else None,
                sha,
            )
        except ArtifactVerificationFailed:
            artifact_sha_mismatch = True
            definition = None
        artifact_commands, artifact_profiles = _artifact_snapshots(definition)
        current_commands = {}
        for name in artifact_commands:
            info = contracts.get(name)
            if info is not None:
                current_commands[name] = info.execution_fingerprint
        current_profiles = {}
        for profile_id in artifact_profiles:
            policy = profiles.policy(profile_id)
            if policy is not None:
                current_profiles[profile_id] = policy.fingerprint()
        health, reasons = evaluate_health(
            enabled=bool(row.get("enabled")),
            artifact=ref,
            artifact_present=definition is not None,
            validation=_validation_record(artifact_row.get("validation") if artifact_row else None),
            current_contract_fingerprints=current_commands,
            artifact_contract_fingerprints=artifact_commands,
            current_profile_fingerprints=current_profiles,
            artifact_profile_fingerprints=artifact_profiles,
            stored_profile_fingerprint=(
                artifact_row.get("profile_fingerprint") if artifact_row else None
            ),
            artifact_sha_mismatch=artifact_sha_mismatch,
        )
        records.append(
            ActivationHealthRecord(
                activation_id=row.get("activation_id", ""),
                playbook_id=row.get("playbook_id", ""),
                scope=row.get("scope", ""),
                scope_identifier=row.get("scope_identifier") or "",
                enabled=bool(row.get("enabled")),
                active_artifact_sha256=sha,
                health=health,
                reasons=reasons,
                activated_at=row.get("activated_at"),
                activated_by=row.get("activated_by"),
            )
        )
    return records
