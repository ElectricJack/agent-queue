"""The durable, secret-free resume record.

One JSON file — ``~/.agent-queue/install-state.json`` by default — carries the
installer version, the target version, the observed platform facts, the
selected capabilities, each step's terminal state and the identifiers of every
resource the installer owns or reused.  It carries nothing else: the contract
forbids credentials, raw DSNs, device codes, shell history, project paths and
task data, and :func:`src.install.redaction.assert_secret_free` enforces that
at write time rather than at review time.

The record exists so that a rerun is the normal recovery mechanism.  Rerunning
recomputes preconditions, reuses matching owned resources, and stops at the
first unsatisfied step; ``--restart-from`` invalidates one step and its
dependents; a record written by a different installer version is reported, not
silently deleted.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .platform import PlatformFacts
from .redaction import assert_secret_free, redact
from .results import ResourceRecord, StepResult, StepState, merge_resource

if TYPE_CHECKING:  # pragma: no cover - import cycle: lifecycle reads this module
    from .lifecycle import UpgradeRecord

#: Bumped when the on-disk record changes shape.  A record written by a newer
#: schema is never rewritten in place.
STATE_SCHEMA_VERSION = 1

DEFAULT_STATE_FILENAME = "install-state.json"

#: Environment override, primarily so tests and a packaged installer can point
#: at a disposable directory without touching an operator's real one.
STATE_DIR_ENV = "AQ_INSTALL_STATE_DIR"
HOME_ENV = "AQ_HOME"


def default_state_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve the directory the resume record lives in."""
    environ = os.environ if environ is None else environ
    override = (environ.get(STATE_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    home = (environ.get(HOME_ENV) or "").strip()
    if home:
        return Path(home).expanduser()
    return Path(os.path.expanduser("~/.agent-queue"))


def default_state_path(environ: Mapping[str, str] | None = None) -> Path:
    return default_state_dir(environ) / DEFAULT_STATE_FILENAME


class StateError(RuntimeError):
    """The record on disk cannot be used as-is."""


class IncompatibleStateError(StateError):
    """The record was written by an incompatible installer or schema.

    Deliberately its own type: the contract requires an explicit repair plan
    for a version-incompatible record, so the engine must be able to tell this
    apart from an unreadable file.
    """


@dataclass(slots=True)
class StepRecord:
    """What a previous run concluded about one step."""

    step_id: str
    state: StepState
    summary: str = ""
    remediation: str | None = None
    retryable: bool = True
    updated_at: str | None = None
    input_schema_version: int = 1

    @property
    def satisfied(self) -> bool:
        return self.state in (StepState.SUCCEEDED, StepState.SKIPPED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "state": self.state.value,
            "summary": self.summary,
            "remediation": self.remediation,
            "retryable": self.retryable,
            "updated_at": self.updated_at,
            "input_schema_version": self.input_schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> StepRecord:
        return cls(
            step_id=str(payload["step_id"]),
            state=StepState(str(payload["state"])),
            summary=str(payload.get("summary") or ""),
            remediation=payload.get("remediation"),
            retryable=bool(payload.get("retryable", True)),
            updated_at=payload.get("updated_at"),
            input_schema_version=int(payload.get("input_schema_version", 1)),
        )


@dataclass(slots=True)
class InstallState:
    """The resume record."""

    installer_version: str
    target_version: str
    schema_version: int = STATE_SCHEMA_VERSION
    platform: dict[str, Any] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    steps: dict[str, StepRecord] = field(default_factory=dict)
    resources: dict[tuple[str, str], ResourceRecord] = field(default_factory=dict)
    #: The version transition this record is in the middle of, if any.  It is
    #: written *before* the first step of an upgrade runs and marked complete
    #: only when the run reaches ``ready``, so an installer that is killed
    #: halfway leaves an ``in_progress`` record the next run can resume from.
    upgrade: UpgradeRecord | None = None
    created_at: str | None = None
    updated_at: str | None = None

    # -- queries -----------------------------------------------------------
    def record_for(self, step_id: str) -> StepRecord | None:
        return self.steps.get(step_id)

    def completed_states(self) -> dict[str, str]:
        return {step_id: record.state.value for step_id, record in self.steps.items()}

    def owned_resources(self) -> tuple[ResourceRecord, ...]:
        return tuple(record for record in self.resources.values() if record.owned)

    # -- mutations ---------------------------------------------------------
    def apply(self, result: StepResult, *, now: str, input_schema_version: int = 1) -> None:
        """Fold one step result into the record, deduplicating resources.

        A resource already in the record keeps its ownership when the newer
        report would lower it: the record is what a later ``aq uninstall``
        reads, and a step that finds AQ's own earlier handiwork already present
        must not turn it into something the host brought.
        """
        self.steps[result.step_id] = StepRecord(
            step_id=result.step_id,
            state=result.state,
            summary=result.summary,
            remediation=result.remediation,
            retryable=result.retryable,
            updated_at=now,
            input_schema_version=input_schema_version,
        )
        for resource in result.resources:
            previous = self.resources.get(resource.key)
            self.resources[resource.key] = (
                resource if previous is None else merge_resource(previous, resource)
            )
        self.updated_at = now

    def invalidate(self, step_ids: Iterable[str], *, now: str) -> tuple[str, ...]:
        """Forget the named steps so the next run re-executes them.

        Resources are *not* dropped: they are the record of what exists on the
        host, and forgetting them is what would make a rerun duplicate them.
        Removing a resource is an explicit uninstall/repair action, not a side
        effect of restarting a step.
        """
        removed = tuple(step_id for step_id in step_ids if step_id in self.steps)
        for step_id in removed:
            del self.steps[step_id]
        if removed:
            self.updated_at = now
        return removed

    def set_platform(self, facts: PlatformFacts) -> None:
        self.platform = facts.to_dict()

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "installer_version": self.installer_version,
            "target_version": self.target_version,
            "platform": dict(self.platform),
            "capabilities": list(self.capabilities),
            "steps": [record.to_dict() for record in self.steps.values()],
            "resources": [record.to_dict() for record in self.resources.values()],
            "upgrade": self.upgrade.to_dict() if self.upgrade is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> InstallState:
        schema_version = int(payload.get("schema_version", 0))
        if schema_version != STATE_SCHEMA_VERSION:
            raise IncompatibleStateError(
                f"install state schema {schema_version} cannot be read by this installer "
                f"(expected {STATE_SCHEMA_VERSION})"
            )
        state = cls(
            installer_version=str(payload.get("installer_version") or ""),
            target_version=str(payload.get("target_version") or ""),
            schema_version=schema_version,
            platform=dict(payload.get("platform") or {}),
            capabilities=[str(value) for value in payload.get("capabilities") or []],
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )
        for row in payload.get("steps") or []:
            record = StepRecord.from_dict(row)
            state.steps[record.step_id] = record
        for row in payload.get("resources") or []:
            resource = ResourceRecord.from_dict(row)
            state.resources[resource.key] = resource
        upgrade = payload.get("upgrade")
        if isinstance(upgrade, Mapping):
            from .lifecycle import UpgradeRecord

            state.upgrade = UpgradeRecord.from_dict(upgrade)
        return state


def load_state(path: Path) -> InstallState | None:
    """Read the record at *path*, or None when there is none.

    A file that exists but cannot be parsed raises :class:`StateError` rather
    than being ignored: silently starting from scratch over a corrupt record is
    exactly how an installer duplicates resources.
    """
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"install state at {path} is unreadable: {error}") from error
    if not isinstance(payload, Mapping):
        raise StateError(f"install state at {path} is not a JSON object")
    return InstallState.from_dict(payload)


def save_state(state: InstallState, path: Path, *, now: str) -> Path:
    """Write the record atomically, owner-readable, and secret-free.

    The payload is redacted first and then *checked*: redaction is the
    convenience and the check is the guarantee, so a future field that carries
    material fails the write instead of shipping it to disk.
    """
    state.created_at = state.created_at or now
    state.updated_at = now
    payload = redact(state.to_dict())
    assert_secret_free(payload, context=f"install state at {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".install-state-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=False)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path


def assert_compatible(state: InstallState, *, installer_version: str) -> None:
    """Refuse a record written by a different installer version.

    "A version-incompatible record requires an explicit repair plan, not
    automatic deletion" — so this raises with the two supported ways forward
    instead of removing the file.
    """
    if state.installer_version and state.installer_version != installer_version:
        raise IncompatibleStateError(
            f"install state was written by installer {state.installer_version}, "
            f"this is {installer_version}; rerun with --restart-from <step> to redo part "
            "of it, or --fresh to start a new record (the existing one is left in place)"
        )
