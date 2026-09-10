"""Repair, upgrade and uninstall — the lifecycle after the first install.

``aq install`` gets a machine to *ready*.  This module owns what happens to
that machine afterwards, and it owns it in one place because all three actions
answer the same question from the same evidence: **what does the installer own
on this host?**  That evidence is the resume record's resource list
(:class:`~src.install.results.ResourceRecord`), where ``owned`` is the boundary
the contract draws — a resource ``aq install`` created may be reconciled or
removed, a resource it found and reused never is.

Three things live here:

**Repair** (:class:`LifecycleMode`) is a rerun that does not take a completed
step's word for it.  The engine already revalidates a completed step through
its read-only ``verify``; a step with no verifier is normally carried forward
untouched, which is right for a rerun and wrong for a repair.  In repair mode
those steps re-execute, reusing the recorded resources, so the install is
reconciled against the host rather than against the record.

**Upgrade** is a repair with a version transition attached, and the transition
is *durable*: :class:`UpgradeRecord` is written to the resume record before the
first step runs and marked complete only when the run reaches ``ready``.  A
process killed halfway leaves an ``in_progress`` record behind, and the next
run — of any mode — finds it, says so, and resumes.  Nothing is rolled back
and nothing is deleted: the contract wants an interrupted upgrade to resume
over resources named in the ownership record, not to unwind them.

**Uninstall** is planned before anything is removed, and the plan is the
product: every recorded resource is classified into remove / keep / manual with
the reason, so a human sees what would go and what would not *before* saying
yes.  The default is deliberately narrow — the AQ runtime and the installer's
own records — and everything that could destroy work (configuration, the data
directory, the PostgreSQL database and role) needs its own explicit flag.
Anything AQ installed but does not exclusively own — a provider CLI, a Homebrew
formula, a PostgreSQL server package — is reported with the command to remove
it by hand and is never removed automatically: `tmux` is AQ's prerequisite and
somebody else's editor multiplexer.

Everything in the planning half is pure: no clock, no filesystem, no network.
The execution half takes its filesystem, command runner and SQL executor as
parameters, so the whole matrix is provable without a machine to uninstall.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .command import CommandOutput, CommandRunner, run_command
from .postgres import (
    IDENTIFIER_RE,
    RESOURCE_CREDENTIAL,
    RESOURCE_DATABASE,
    RESOURCE_ROLE,
    SqlError,
    SqlExecutor,
    backup_path_for,
)
from .results import InstallOutcome, ResourceRecord, exit_code
from .state import InstallState

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


class LifecycleMode(str, Enum):
    """What a run of the engine is for.

    The three modes share one engine, one registry and one resume record; they
    differ only in how much a completed step is trusted and whether a version
    transition is being recorded.
    """

    INSTALL = "install"
    REPAIR = "repair"
    UPGRADE = "upgrade"

    @property
    def reconciles(self) -> bool:
        """True when a completed step that cannot be verified is re-executed.

        A plain install trusts its record — that is what makes a rerun cheap.
        A repair does not, because "the record says it is done" is exactly the
        claim a repair exists to doubt.
        """
        return self in (LifecycleMode.REPAIR, LifecycleMode.UPGRADE)

    @property
    def adopts_foreign_record(self) -> bool:
        """True when a record written by another installer version is adopted.

        The contract says a version-incompatible record "requires an explicit
        repair plan, not automatic deletion".  ``--repair`` and ``--upgrade``
        *are* that explicit plan: they keep the resources, re-verify every
        step and re-stamp the version.  A plain rerun still refuses.
        """
        return self.reconciles


# ---------------------------------------------------------------------------
# The upgrade transaction
# ---------------------------------------------------------------------------

UPGRADE_IN_PROGRESS = "in_progress"
UPGRADE_COMPLETED = "completed"


@dataclass(slots=True)
class UpgradeRecord:
    """A version transition that has started and may not have finished.

    Persisted in the resume record so that "the installer was killed during an
    upgrade" is a fact the next run can read rather than something it has to
    infer from a half-updated host.
    """

    from_version: str
    to_version: str
    state: str = UPGRADE_IN_PROGRESS
    started_at: str | None = None
    finished_at: str | None = None
    attempts: int = 1

    @property
    def in_progress(self) -> bool:
        return self.state == UPGRADE_IN_PROGRESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> UpgradeRecord:
        return cls(
            from_version=str(payload.get("from_version") or ""),
            to_version=str(payload.get("to_version") or ""),
            state=str(payload.get("state") or UPGRADE_IN_PROGRESS),
            started_at=payload.get("started_at"),
            finished_at=payload.get("finished_at"),
            attempts=int(payload.get("attempts", 1)),
        )


@dataclass(frozen=True, slots=True)
class UpgradeDecision:
    """What :func:`plan_upgrade` concluded, and what to tell the operator."""

    record: UpgradeRecord | None
    resumed: bool = False
    started: bool = False
    superseded: UpgradeRecord | None = None
    messages: tuple[str, ...] = ()


def plan_upgrade(
    state: InstallState,
    *,
    mode: LifecycleMode,
    from_version: str,
    target_version: str,
    now: str,
) -> UpgradeDecision:
    """Decide this run's upgrade transaction from the record and the mode.

    *from_version* is the version the record on disk was written by — not the
    version now stamped on ``state``.  Adoption re-stamps that field, so
    reading it here would make every upgrade look like a no-op.

    Recovering an interrupted upgrade is *not* gated on ``--upgrade``: a plain
    rerun is the documented recovery path for everything else the installer
    does, and an operator whose upgrade was killed should not have to know
    which flag restarted it.  What ``--upgrade`` adds is permission to *begin*
    a transition, and to supersede one aimed at a version nobody wants any
    more.
    """
    existing = state.upgrade
    if existing is not None and existing.in_progress:
        if existing.to_version == target_version:
            resumed = UpgradeRecord(
                from_version=existing.from_version,
                to_version=existing.to_version,
                state=UPGRADE_IN_PROGRESS,
                started_at=existing.started_at,
                attempts=existing.attempts + 1,
            )
            return UpgradeDecision(
                record=resumed,
                resumed=True,
                messages=(
                    (
                        f"resuming an interrupted upgrade {existing.from_version} -> "
                        f"{existing.to_version} started at {existing.started_at or 'an unknown time'} "
                        f"(attempt {resumed.attempts})"
                    ),
                ),
            )
        if mode is LifecycleMode.UPGRADE:
            started = UpgradeRecord(
                from_version=existing.from_version,
                to_version=target_version,
                started_at=now,
            )
            return UpgradeDecision(
                record=started,
                started=True,
                superseded=existing,
                messages=(
                    (
                        f"superseding an interrupted upgrade to {existing.to_version}; this run "
                        f"targets {target_version} from {existing.from_version}"
                    ),
                ),
            )
        return UpgradeDecision(
            record=existing,
            messages=(
                (
                    f"an interrupted upgrade to {existing.to_version} is still recorded; "
                    f"this run targets {target_version}. Run `aq install --upgrade` to "
                    "retarget it."
                ),
            ),
        )

    if mode is not LifecycleMode.UPGRADE:
        return UpgradeDecision(record=existing)

    if from_version == target_version:
        return UpgradeDecision(
            record=existing,
            messages=(
                (
                    f"already at {target_version}; reconciling the installation instead of "
                    "recording a version transition"
                ),
            ),
        )
    started = UpgradeRecord(from_version=from_version, to_version=target_version, started_at=now)
    return UpgradeDecision(
        record=started,
        started=True,
        messages=(f"upgrading {from_version} -> {target_version}",),
    )


def complete_upgrade(state: InstallState, *, now: str) -> UpgradeRecord | None:
    """Mark an in-progress transition finished.  Returns it, or None."""
    record = state.upgrade
    if record is None or not record.in_progress:
        return None
    record.state = UPGRADE_COMPLETED
    record.finished_at = now
    return record


# ---------------------------------------------------------------------------
# Uninstall planning
# ---------------------------------------------------------------------------


class RemovalScope(str, Enum):
    """The four things an uninstall may be asked to remove.

    Only :attr:`RUNTIME` is on by default.  The other three destroy work that
    is not the installer's to throw away, so each is its own flag and its own
    confirmation.
    """

    RUNTIME = "runtime"
    CONFIG = "config"
    DATA = "data"
    DATABASE = "database"

    @property
    def destructive(self) -> bool:
        return self is not RemovalScope.RUNTIME


#: What ``aq uninstall`` does with no scope flags: stop the runtime it started,
#: undo the shell edit it made, and forget its own records.
DEFAULT_SCOPES: frozenset[RemovalScope] = frozenset({RemovalScope.RUNTIME})

DESTRUCTIVE_SCOPES: tuple[RemovalScope, ...] = (
    RemovalScope.CONFIG,
    RemovalScope.DATA,
    RemovalScope.DATABASE,
)

#: The synthetic resource kind for the resume record itself.  It is not written
#: by a step — it *is* the file the steps are written into — but uninstall has
#: to be able to name it, plan it and report it like everything else.
KIND_INSTALL_RECORD = "install-record"

#: Which scope owns each recorded resource kind.  A kind that is not in this
#: table is never removed: an adapter that starts recording something new gets
#: "left in place, unknown kind" rather than deletion by default.
SCOPE_BY_KIND: Mapping[str, RemovalScope] = {
    "daemon": RemovalScope.RUNTIME,
    "shell-profile": RemovalScope.RUNTIME,
    KIND_INSTALL_RECORD: RemovalScope.RUNTIME,
    "config": RemovalScope.CONFIG,
    "directory": RemovalScope.DATA,
    RESOURCE_DATABASE: RemovalScope.DATABASE,
    RESOURCE_ROLE: RemovalScope.DATABASE,
}

#: Kinds AQ installed but does not exclusively own.  Removing one could break
#: software that has nothing to do with AQ, and none of these has a removal
#: command AQ is entitled to guess, so uninstall reports them instead.
MANUAL_KINDS: Mapping[str, str] = {
    "provider-cli": (
        "Installed for AQ, but it is a general-purpose CLI: remove it with the "
        "provider's own uninstaller if you no longer want it."
    ),
    "brew-formula": (
        "Installed with Homebrew and probably shared with the rest of this machine: "
        "`brew uninstall {id}` removes it once you are sure nothing else needs it."
    ),
    "postgres-server": (
        "A PostgreSQL server serves more than AQ. Remove the package with your platform's "
        "package manager once you have confirmed no other database on it is in use."
    ),
}

#: Kinds uninstall never touches at all, whatever is selected.
NEVER_REMOVE: Mapping[str, str] = {
    RESOURCE_CREDENTIAL: (
        "AQ never creates, reads, moves or deletes a credential store; the reference is "
        "removed with the configuration that holds it."
    ),
}


class RemovalAction(str, Enum):
    REMOVE = "remove"
    KEEP = "keep"
    MANUAL = "manual"


#: The order removals are executed in.  The daemon stops before the database it
#: is connected to goes away, the configuration outlives the database drop that
#: reads its settings, and the resume record — the only durable evidence of
#: what is being removed — is deleted last.
_REMOVAL_ORDER: tuple[str, ...] = (
    "daemon",
    RESOURCE_DATABASE,
    RESOURCE_ROLE,
    "shell-profile",
    "config",
    "directory",
    KIND_INSTALL_RECORD,
)


@dataclass(frozen=True, slots=True)
class RemovalItem:
    """One recorded resource and what this uninstall intends to do with it."""

    resource: ResourceRecord
    action: RemovalAction
    scope: RemovalScope | None
    reason: str
    hint: str = ""

    @property
    def kind(self) -> str:
        return self.resource.kind

    @property
    def id(self) -> str:
        return self.resource.id

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "action": self.action.value,
            "scope": self.scope.value if self.scope else None,
            "reason": self.reason,
            "hint": self.hint,
            "owned": self.resource.owned,
        }


@dataclass(frozen=True, slots=True)
class UninstallPlan:
    """What an uninstall would do, before it does any of it."""

    scopes: frozenset[RemovalScope]
    items: tuple[RemovalItem, ...]
    state_path: str | None = None

    def _with(self, action: RemovalAction) -> tuple[RemovalItem, ...]:
        return tuple(item for item in self.items if item.action is action)

    @property
    def removals(self) -> tuple[RemovalItem, ...]:
        return self._with(RemovalAction.REMOVE)

    @property
    def kept(self) -> tuple[RemovalItem, ...]:
        return self._with(RemovalAction.KEEP)

    @property
    def manual(self) -> tuple[RemovalItem, ...]:
        return self._with(RemovalAction.MANUAL)

    @property
    def destructive_scopes(self) -> tuple[RemovalScope, ...]:
        """The destructive scopes this plan would actually act on.

        Selecting ``--remove-database`` on a host whose database was reused is
        not a destructive request: there is nothing owned to drop, so nothing
        is confirmed.
        """
        selected = {item.scope for item in self.removals if item.scope and item.scope.destructive}
        return tuple(scope for scope in DESTRUCTIVE_SCOPES if scope in selected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scopes": sorted(scope.value for scope in self.scopes),
            "state_path": self.state_path,
            "items": [item.to_dict() for item in self.items],
            "destructive_scopes": [scope.value for scope in self.destructive_scopes],
        }


def plan_uninstall(
    state: InstallState,
    *,
    scopes: frozenset[RemovalScope] = DEFAULT_SCOPES,
    state_path: Path | str | None = None,
) -> UninstallPlan:
    """Classify every recorded resource against the selected *scopes*.

    The classification is a chain of refusals with one acceptance at the end,
    in that order on purpose: a resource has to survive every reason to keep
    it before removal is even considered.
    """
    records: list[ResourceRecord] = list(state.resources.values())
    if state_path is not None:
        records.append(
            ResourceRecord(
                kind=KIND_INSTALL_RECORD,
                id=str(state_path),
                owned=True,
                detail={"note": "the installer's own resume record"},
            )
        )

    items = [_classify(record, scopes) for record in records]
    order = {kind: index for index, kind in enumerate(_REMOVAL_ORDER)}
    items.sort(key=lambda item: (order.get(item.kind, len(order)), item.kind, item.id))
    return UninstallPlan(
        scopes=frozenset(scopes),
        items=tuple(items),
        state_path=str(state_path) if state_path is not None else None,
    )


def _classify(record: ResourceRecord, scopes: frozenset[RemovalScope]) -> RemovalItem:
    if not record.owned:
        return RemovalItem(
            record,
            RemovalAction.KEEP,
            None,
            "found on this host and reused; `aq install` did not create it",
        )
    if record.kind in NEVER_REMOVE:
        return RemovalItem(record, RemovalAction.KEEP, None, NEVER_REMOVE[record.kind])
    if record.kind in MANUAL_KINDS:
        return RemovalItem(
            record,
            RemovalAction.MANUAL,
            None,
            "installed by AQ but shared with the rest of this machine",
            hint=MANUAL_KINDS[record.kind].format(id=record.id),
        )
    scope = SCOPE_BY_KIND.get(record.kind)
    if scope is None:
        return RemovalItem(
            record,
            RemovalAction.KEEP,
            None,
            f"no removal is defined for resources of kind '{record.kind}'",
        )
    if scope not in scopes:
        return RemovalItem(
            record,
            RemovalAction.KEEP,
            scope,
            f"not selected; pass --remove-{scope.value} to remove it",
        )
    return RemovalItem(
        record, RemovalAction.REMOVE, scope, f"owned by AQ and in scope {scope.value}"
    )


# ---------------------------------------------------------------------------
# Uninstall execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemovalOutcome:
    """What actually happened to one planned removal."""

    item: RemovalItem
    removed: bool
    summary: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = self.item.to_dict()
        payload.update({"removed": self.removed, "summary": self.summary, "error": self.error})
        return payload


@dataclass(frozen=True, slots=True)
class UninstallResult:
    """The complete machine-readable result of one ``aq uninstall`` run."""

    outcome: InstallOutcome
    dry_run: bool
    plan: UninstallPlan
    outcomes: tuple[RemovalOutcome, ...] = ()
    messages: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return exit_code(self.outcome)

    @property
    def failures(self) -> tuple[RemovalOutcome, ...]:
        return tuple(row for row in self.outcomes if row.error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "outcome": self.outcome.value,
            "exit_code": self.exit_code,
            "dry_run": self.dry_run,
            "plan": self.plan.to_dict(),
            "removed": [row.to_dict() for row in self.outcomes],
            "kept": [item.to_dict() for item in self.plan.kept],
            "manual": [item.to_dict() for item in self.plan.manual],
            "messages": list(self.messages),
        }


#: A handler turns one planned removal into an outcome.  Handlers are keyed by
#: resource kind so a new kind is added by registering one, not by editing a
#: dispatch chain.
RemovalHandler = Callable[[RemovalItem], RemovalOutcome]


def execute_uninstall(
    plan: UninstallPlan,
    handlers: Mapping[str, RemovalHandler],
    *,
    dry_run: bool = False,
    messages: Sequence[str] = (),
) -> UninstallResult:
    """Run *plan*'s removals through *handlers* in the documented order.

    A removal that fails does **not** stop the run.  The alternative — abort at
    the first error — leaves a host half-uninstalled with no record of which
    half, which is worse than finishing and reporting every failure at once.
    The resume record is the last thing removed, so a failed run leaves the
    evidence behind for the next attempt.
    """
    if dry_run:
        return UninstallResult(
            outcome=InstallOutcome.READY,
            dry_run=True,
            plan=plan,
            messages=tuple(messages),
        )

    outcomes: list[RemovalOutcome] = []
    for item in plan.removals:
        handler = handlers.get(item.kind)
        if handler is None:
            outcomes.append(
                RemovalOutcome(
                    item,
                    removed=False,
                    summary=f"no handler is registered for '{item.kind}'",
                    error=f"unsupported resource kind: {item.kind}",
                )
            )
            continue
        try:
            outcomes.append(handler(item))
        except Exception as error:  # noqa: BLE001 - a handler's crash is this item's failure
            outcomes.append(
                RemovalOutcome(
                    item,
                    removed=False,
                    summary=f"{type(error).__name__}: {error}",
                    error=f"{type(error).__name__}: {error}",
                )
            )

    failed = any(row.error for row in outcomes)
    return UninstallResult(
        outcome=InstallOutcome.FAILED if failed else InstallOutcome.READY,
        dry_run=False,
        plan=plan,
        outcomes=tuple(outcomes),
        messages=tuple(messages),
    )


# ---------------------------------------------------------------------------
# The default handlers
# ---------------------------------------------------------------------------


def strip_marked_block(text: str, begin: str, end: str) -> tuple[str, bool]:
    """Remove every ``begin``..``end`` block from *text*.

    Pure and line-based: the installer writes the block with its own markers,
    so the same markers are what identify it for removal.  Text outside the
    markers — the operator's own shell configuration — is returned untouched,
    which is the whole point of writing a marked block in the first place.
    """
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    removed = False
    inside = False
    for line in lines:
        stripped = line.strip()
        if not inside and stripped == begin:
            inside = True
            removed = True
            continue
        if inside:
            if stripped == end:
                inside = False
            continue
        kept.append(line)
    if inside:
        # An unterminated block: everything after the marker was ours, and
        # dropping it is what "remove the block AQ wrote" means.
        removed = True
    return "".join(kept), removed


def _daemon_handler(runner: CommandRunner, which: Callable[[str], str | None]) -> RemovalHandler:
    def handle(item: RemovalItem) -> RemovalOutcome:
        executable = which("aq")
        if not executable:
            return RemovalOutcome(
                item,
                removed=False,
                summary="`aq` is not on PATH, so the daemon could not be stopped",
                error="the `aq` command is not on PATH",
            )
        output: CommandOutput = runner([executable, "stop"], timeout=60.0)
        if output.ok:
            return RemovalOutcome(item, removed=True, summary=f"stopped the daemon at {item.id}")
        # `aq stop` against a daemon that is not running is the state we want,
        # not a failure to reach it.
        return RemovalOutcome(
            item,
            removed=True,
            summary=f"the daemon at {item.id} was not running ({output.message()})",
        )

    return handle


def _shell_profile_handler() -> RemovalHandler:
    from .macos import BEGIN_MARKER, END_MARKER

    def handle(item: RemovalItem) -> RemovalOutcome:
        path = Path(item.id)
        if not path.is_file():
            return RemovalOutcome(item, removed=True, summary=f"{path} no longer exists")
        detail = item.resource.detail
        begin = str(detail.get("marker") or BEGIN_MARKER)
        end = str(detail.get("marker_end") or END_MARKER)
        original = path.read_text(encoding="utf-8", errors="replace")
        updated, removed = strip_marked_block(original, begin, end)
        if not removed:
            return RemovalOutcome(
                item,
                removed=False,
                summary=f"{path} no longer contains the block `aq install` added",
            )
        path.write_text(updated, encoding="utf-8")
        return RemovalOutcome(
            item, removed=True, summary=f"removed the block `aq install` added to {path}"
        )

    return handle


def _config_handler() -> RemovalHandler:
    def handle(item: RemovalItem) -> RemovalOutcome:
        path = Path(item.id)
        if not path.exists():
            return RemovalOutcome(item, removed=True, summary=f"{path} no longer exists")
        # Back the file up before deleting it.  A configuration is small, was
        # hand-edited more often than not, and an uninstall an operator regrets
        # should be survivable by copying one file back.
        backup = backup_path_for(path)
        backup.write_bytes(path.read_bytes())
        path.unlink()
        return RemovalOutcome(
            item, removed=True, summary=f"removed {path} (saved as {backup.name})"
        )

    return handle


def _directory_handler(*, allowed_roots: Sequence[Path]) -> RemovalHandler:
    """Remove a directory the installer created, and nothing else.

    The guard is not paranoia about a hostile record: it is about a record that
    is merely *stale*.  ``install-state.json`` is a plain file an operator may
    have edited, moved between machines or restored from a backup, and a
    ``directory`` row that no longer means what it meant is the difference
    between removing ``~/.agent-queue`` and removing ``~``.
    """
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]

    def handle(item: RemovalItem) -> RemovalOutcome:
        path = Path(item.id).expanduser()
        if not path.exists():
            return RemovalOutcome(item, removed=True, summary=f"{path} no longer exists")
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in roots):
            return RemovalOutcome(
                item,
                removed=False,
                summary=f"{resolved} is outside AQ's install home and was left alone",
                error=(
                    f"refusing to remove {resolved}: it is not inside "
                    f"{', '.join(str(root) for root in roots) or 'any known AQ directory'}"
                ),
            )
        if resolved.is_symlink():
            resolved.unlink()
            return RemovalOutcome(item, removed=True, summary=f"removed the symlink {resolved}")
        shutil.rmtree(resolved)
        return RemovalOutcome(item, removed=True, summary=f"removed {resolved} and its contents")

    return handle


def _sql_handler(
    executor: SqlExecutor | None,
    *,
    statement: str,
    noun: str,
    remediation: str,
) -> RemovalHandler:
    def handle(item: RemovalItem) -> RemovalOutcome:
        if not IDENTIFIER_RE.match(item.id):
            return RemovalOutcome(
                item,
                removed=False,
                summary=f"the recorded {noun} name is not a plain identifier",
                error=f"refusing to drop {noun} {item.id!r}: it is not a plain SQL identifier",
            )
        if executor is None:
            return RemovalOutcome(
                item,
                removed=False,
                summary=f"no PostgreSQL administrator connection, so {item.id} was left alone",
                error=remediation,
            )
        try:
            executor.execute(statement.format(id=item.id))
        except SqlError as error:
            return RemovalOutcome(
                item,
                removed=False,
                summary=f"could not drop {noun} {item.id}",
                error=str(error),
            )
        return RemovalOutcome(item, removed=True, summary=f"dropped {noun} {item.id}")

    return handle


def _install_record_handler() -> RemovalHandler:
    def handle(item: RemovalItem) -> RemovalOutcome:
        path = Path(item.id)
        if not path.exists():
            return RemovalOutcome(item, removed=True, summary=f"{path} no longer exists")
        path.unlink()
        return RemovalOutcome(item, removed=True, summary=f"removed the resume record {path}")

    return handle


def default_handlers(
    *,
    home: Path,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    admin: SqlExecutor | None = None,
    admin_remediation: str = "",
    allowed_roots: Sequence[Path] | None = None,
) -> dict[str, RemovalHandler]:
    """Build the removal handlers for a real host.

    Every dependency that touches the world is a parameter: the command runner
    that stops the daemon, the ``which`` that finds ``aq``, the administrator
    connection that drops a database, and the roots a directory removal is
    confined to.
    """
    import shutil as _shutil

    home = Path(home).expanduser()
    remediation = admin_remediation or (
        "No PostgreSQL administrator connection is available on this host. Drop the AQ "
        "database and role yourself, or rerun `aq uninstall --remove-database` where an "
        "administrator connection is configured."
    )
    return {
        "daemon": _daemon_handler(runner or run_command, which or _shutil.which),
        "shell-profile": _shell_profile_handler(),
        "config": _config_handler(),
        "directory": _directory_handler(allowed_roots=allowed_roots or (home,)),
        RESOURCE_DATABASE: _sql_handler(
            admin,
            statement='DROP DATABASE IF EXISTS "{id}"',
            noun="database",
            remediation=remediation,
        ),
        RESOURCE_ROLE: _sql_handler(
            admin,
            statement='DROP ROLE IF EXISTS "{id}"',
            noun="role",
            remediation=remediation,
        ),
        KIND_INSTALL_RECORD: _install_record_handler(),
    }


__all__ = [
    "DEFAULT_SCOPES",
    "DESTRUCTIVE_SCOPES",
    "KIND_INSTALL_RECORD",
    "MANUAL_KINDS",
    "NEVER_REMOVE",
    "SCOPE_BY_KIND",
    "UPGRADE_COMPLETED",
    "UPGRADE_IN_PROGRESS",
    "LifecycleMode",
    "RemovalAction",
    "RemovalHandler",
    "RemovalItem",
    "RemovalOutcome",
    "RemovalScope",
    "UninstallPlan",
    "UninstallResult",
    "UpgradeDecision",
    "UpgradeRecord",
    "complete_upgrade",
    "default_handlers",
    "execute_uninstall",
    "plan_uninstall",
    "plan_upgrade",
    "strip_marked_block",
]
