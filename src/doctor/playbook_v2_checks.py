"""``playbooks.*`` doctor checks for Playbook V2 storage (child plan §12.2).

``playbooks.artifact_integrity``: every enabled activation's
``active_artifact_sha256`` must have an artifact row, a file on disk, and a
file whose contents hash to the name it is stored under.

``playbooks.pending_event_replay_policy``: the one production caller that hands
live activation health to ``PlaybooksConfig.validate()``.  A config file is
validated before any database is open, so ``AppConfig.validate()`` can only
check the vocabulary of ``v2_pending_event_replay_on_activation``; whether
``automatic`` points at an activation that may not auto-consume a backlog is a
question about *rows*, and this is where the daemon asks it.

``playbooks.activation_stale``: every enabled activation must still agree with
the live registries — the command execution contracts *and* the capability
profiles it was compiled against — and must still read the exact artifact it
activated.  The design spec makes a changed capability profile stale an
activation exactly like a changed command contract does.  A hash mismatch is
also surfaced here so this health read agrees with ``artifact_integrity``; a
corrupt file is a rebuild while moved fingerprints need recompile-and-reactivate.

There is deliberately **no fix**.  A missing or mutated artifact is a rebuild
decision (Package 6 recompiles from source and Package 5 activates the
result); a doctor that "repaired" it could only do so by deleting the
activation or by trusting whatever bytes it found, and both are worse than
telling an operator exactly which playbook and which hash went bad.

Mirrors ``src/doctor/formula_checks.py``'s shape: a private ``_check_*``
function plus a factory returning the :class:`DoctorCheck` list.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "playbook-v2"
CHECK_ID = "playbooks.artifact_integrity"
STALE_CHECK_ID = "playbooks.activation_stale"
RELEASE_CHECK_ID = "playbooks.stale_artifacts"
REPLAY_POLICY_CHECK_ID = "playbooks.pending_event_replay_policy"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


async def _check_artifact_integrity(ctx: DoctorContext) -> CheckResult:
    playbooks = getattr(ctx.config, "playbooks", None)
    if playbooks is None or not getattr(playbooks, "enabled", False):
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="playbooks.enabled is false; V2 artifact storage is inert",
        )
    db = ctx.db
    if db is None or not hasattr(db, "list_playbook_activations"):
        return CheckResult(
            id=CHECK_ID, severity=Severity.INFO, detail="V2 artifact tables are not available"
        )

    activations = await db.list_playbook_activations(enabled_only=True)
    faults: list[dict[str, str]] = []
    checked = 0
    for row in activations:
        sha = row.get("active_artifact_sha256")
        playbook_id = row.get("playbook_id", "?")
        if not sha:
            faults.append(
                {"playbook_id": playbook_id, "artifact_sha256": "", "problem": "no_artifact"}
            )
            continue
        checked += 1
        path = await db.get_playbook_artifact_path(sha)
        if not path:
            faults.append(
                {"playbook_id": playbook_id, "artifact_sha256": sha, "problem": "row_missing"}
            )
            continue
        target = Path(path)
        if not target.is_file():
            faults.append(
                {
                    "playbook_id": playbook_id,
                    "artifact_sha256": sha,
                    "problem": "file_missing",
                    "path": path,
                }
            )
            continue
        try:
            actual = _digest(target)
        except OSError as exc:
            faults.append(
                {
                    "playbook_id": playbook_id,
                    "artifact_sha256": sha,
                    "problem": "unreadable",
                    "path": path,
                    "error": str(exc),
                }
            )
            continue
        if actual != sha:
            faults.append(
                {
                    "playbook_id": playbook_id,
                    "artifact_sha256": sha,
                    "problem": "hash_mismatch",
                    "path": path,
                    "actual_sha256": actual,
                }
            )

    if not faults:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.OK,
            detail=f"{checked} enabled activation(s) resolve to a verified artifact",
            data={"checked": checked},
        )
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"{len(faults)} enabled activation(s) do not resolve to a verified artifact; "
            "rebuild and re-activate them"
        ),
        data={"checked": checked, "count": len(faults), "faults": faults},
    )


async def _lookups(ctx: DoctorContext):
    """The contract and profile registries, from the handler when there is one.

    ``CommandHandler._v2_lookups`` is the daemon's single construction site: it
    owns ``plugin_command_names``, and forgetting that argument classifies a
    plugin tool into the wrong capability namespace — which would move the
    profile fingerprint and make this check report drift it invented itself.
    """
    handler = ctx.handler
    if handler is not None and hasattr(handler, "_v2_lookups"):
        contracts, profiles, _events = await handler._v2_lookups()
        return contracts, profiles
    from src.playbooks.validation import RegistryContractLookup, VaultProfileLookup

    rows = await ctx.db.list_profiles()
    return RegistryContractLookup(), VaultProfileLookup({row.id: row for row in rows})


async def _check_activation_stale(ctx: DoctorContext) -> CheckResult:
    playbooks = getattr(ctx.config, "playbooks", None)
    if playbooks is None or not getattr(playbooks, "enabled", False):
        return CheckResult(
            id=STALE_CHECK_ID,
            severity=Severity.INFO,
            detail="playbooks.enabled is false; V2 artifact storage is inert",
        )
    db = ctx.db
    if db is None or not hasattr(db, "list_playbook_activations"):
        return CheckResult(
            id=STALE_CHECK_ID, severity=Severity.INFO, detail="V2 artifact tables are not available"
        )

    from src.playbooks.activation import ActivationHealth, load_activation_health

    contracts, profiles = await _lookups(ctx)
    records = await load_activation_health(
        db, contracts=contracts, profiles=profiles, enabled_only=True
    )
    stale = [
        {
            "playbook_id": record.playbook_id,
            "scope": record.scope,
            "scope_identifier": record.scope_identifier,
            "artifact_sha256": record.active_artifact_sha256 or "",
            "reasons": [reason.as_dict() for reason in record.reasons],
        }
        for record in records
        if record.health is ActivationHealth.STALE_CONTRACT
        or any(reason.code == "artifact_sha_mismatch" for reason in record.reasons)
    ]
    if not stale:
        return CheckResult(
            id=STALE_CHECK_ID,
            severity=Severity.OK,
            detail=f"{len(records)} enabled activation(s) match the current contracts and profiles",
            data={"checked": len(records)},
        )
    subjects = sorted(
        {
            reason.get("subject") or reason.get("code", "")
            for row in stale
            for reason in row["reasons"]
        }
    )
    return CheckResult(
        id=STALE_CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"{len(stale)} enabled activation(s) have stale contracts, profiles, or artifact "
            f"bytes ({', '.join(subjects)}); rebuild or recompile and re-activate them"
        ),
        data={"checked": len(records), "count": len(stale), "stale": stale},
    )


async def _check_stale_artifacts(ctx: DoctorContext) -> CheckResult:
    """Package 6 §5.5 — the release check, on a live daemon.

    ``playbooks.activation_stale`` asks the *activation health* surface whether
    the rows a daemon is serving are stale.  This asks the complementary
    question the same way CI does: do the **checked-in reviewed artifacts**
    still match the command contracts this build serves?  A contributor who
    changes a command's execution shape without rebuilding the fixtures sees it
    here as well as in `tests/test_playbook_contract_release_check.py`.

    Read-only and offline; it never compiles or activates anything.
    """
    from src.commands.contracts import CONTRACTS
    from src.playbooks.migration import release_check

    handler = getattr(ctx, "handler", None)
    activations = []
    evidence_errors: list[dict[str, str]] = []
    if handler is not None and hasattr(handler, "_release_check_activations"):
        try:
            activations = await handler._release_check_activations(
                evidence_errors=evidence_errors
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Swallowing this to `[]` made the check report a clean fleet from a
            # daemon whose activations it had never read (`prime-zenith-66`).
            activations = []
            evidence_errors.append(
                {"source": "activations", "error": f"{type(exc).__name__}: {exc}"}
            )

    report = release_check(
        contract_registry=CONTRACTS,
        activations=activations,
        evidence_errors=evidence_errors,
    )
    checked = report["checked"]
    if report["success"]:
        return CheckResult(
            id=RELEASE_CHECK_ID,
            severity=Severity.OK,
            detail=(
                f"{len(checked)} reviewed artifact(s) match the contracts they were "
                "compiled against"
            ),
            data={"checked": checked},
        )
    data = {
        "checked": checked,
        "stale": report["stale"],
        "unverified": report.get("unverified", []),
        "evidence_errors": report.get("evidence_errors", []),
        "blocking_reasons": report.get("blocking_reasons", []),
    }
    if not report["stale"]:
        # Nothing drifted; the check simply could not see everything it must
        # compare, and "unknown" is not "clean".
        unreadable = ", ".join(
            sorted(
                {row["source"] for row in data["evidence_errors"]}
                | {row["playbook_id"] or "an unnamed activation" for row in data["unverified"]}
            )
        )
        return CheckResult(
            id=RELEASE_CHECK_ID,
            severity=Severity.WARN,
            detail=(
                f"the release check could not read all of its evidence ({unreadable}); "
                "it cannot certify activations it never compared"
            ),
            data=data,
        )
    named = sorted({f"{row['playbook_id']}:{row['dependency']}" for row in report["stale"]})
    return CheckResult(
        id=RELEASE_CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"{len(report['stale'])} reviewed artifact dependenc(ies) moved since review "
            f"({', '.join(named)}); rebuild the artifact, re-review it, and update the fixture"
        ),
        data=data,
    )


async def _check_pending_event_replay_policy(ctx: DoctorContext) -> CheckResult:
    """``automatic`` replay against a ``question_required`` activation.

    ``PlaybooksConfig.validate(activation_healths=...)`` already knows how to
    refuse that combination and how to name the offending playbooks; without a
    caller that has the rows, the refusal never fires on a running daemon.
    This is that caller — read-only, and with no fix, because the two repairs
    (review the playbook, or set the policy back to ``manual``) are both
    operator decisions.
    """
    playbooks = getattr(ctx.config, "playbooks", None)
    if playbooks is None:
        return CheckResult(
            id=REPLAY_POLICY_CHECK_ID,
            severity=Severity.INFO,
            detail="no playbooks config section",
        )
    policy = getattr(playbooks, "v2_pending_event_replay_on_activation", "manual")
    if policy != "automatic":
        return CheckResult(
            id=REPLAY_POLICY_CHECK_ID,
            severity=Severity.OK,
            detail=(
                f"playbooks.v2_pending_event_replay_on_activation is '{policy}'; "
                "activation never consumes a backlog on its own"
            ),
            data={"policy": policy},
        )
    db = ctx.db
    if db is None or not hasattr(db, "list_playbook_activations"):
        return CheckResult(
            id=REPLAY_POLICY_CHECK_ID,
            severity=Severity.INFO,
            detail="V2 artifact tables are not available",
            data={"policy": policy},
        )

    from src.playbooks.activation import load_activation_health

    contracts, profiles = await _lookups(ctx)
    records = await load_activation_health(db, contracts=contracts, profiles=profiles)
    healths = {record.playbook_id: record.health.value for record in records}
    errors = [
        error
        for error in playbooks.validate(activation_healths=healths)
        if error.field == "v2_pending_event_replay_on_activation"
    ]
    if not errors:
        return CheckResult(
            id=REPLAY_POLICY_CHECK_ID,
            severity=Severity.OK,
            detail=(
                f"'automatic' replay is admissible for {len(healths)} activation(s)"
            ),
            data={"policy": policy, "checked": len(healths)},
        )
    unreviewed = sorted(
        playbook_id
        for playbook_id, health in healths.items()
        if health == "question_required"
    )
    return CheckResult(
        id=REPLAY_POLICY_CHECK_ID,
        severity=Severity.ERROR,
        detail="; ".join(error.message for error in errors),
        data={"policy": policy, "checked": len(healths), "question_required": unreviewed},
    )


def playbook_v2_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id=CHECK_ID, run=_check_artifact_integrity, fix=None, owner=OWNER),
        DoctorCheck(id=STALE_CHECK_ID, run=_check_activation_stale, fix=None, owner=OWNER),
        DoctorCheck(id=RELEASE_CHECK_ID, run=_check_stale_artifacts, fix=None, owner=OWNER),
        DoctorCheck(
            id=REPLAY_POLICY_CHECK_ID,
            run=_check_pending_event_replay_policy,
            fix=None,
            owner=OWNER,
        ),
    ]
