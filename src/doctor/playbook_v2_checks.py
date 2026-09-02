"""``playbooks.*`` doctor checks for Playbook V2 storage (child plan §12.2).

``playbooks.artifact_integrity``: every enabled activation's
``active_artifact_sha256`` must have an artifact row, a file on disk, and a
file whose contents hash to the name it is stored under.

``playbooks.activation_stale``: every enabled activation must still agree with
the live registries — the command execution contracts *and* the capability
profiles it was compiled against.  The design spec makes a changed capability
profile stale an activation exactly like a changed command contract does, and
this is the check that says so out loud; the two are separate ids because they
fail for unrelated reasons and an operator fixes them differently (a corrupt
file is a rebuild, a moved fingerprint is a recompile-and-reactivate).

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


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


async def _check_artifact_integrity(ctx: DoctorContext) -> CheckResult:
    playbooks = getattr(ctx.config, "playbooks", None)
    if playbooks is None or not getattr(playbooks, "v2_storage_enabled", False):
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="playbooks.v2_storage_enabled is false; V2 artifact storage is inert",
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
    if playbooks is None or not getattr(playbooks, "v2_storage_enabled", False):
        return CheckResult(
            id=STALE_CHECK_ID,
            severity=Severity.INFO,
            detail="playbooks.v2_storage_enabled is false; V2 artifact storage is inert",
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
            f"{len(stale)} enabled activation(s) were compiled against a command contract or "
            f"capability profile that has since changed ({', '.join(subjects)}); recompile and "
            "re-activate them"
        ),
        data={"checked": len(records), "count": len(stale), "stale": stale},
    )


def playbook_v2_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id=CHECK_ID, run=_check_artifact_integrity, fix=None, owner=OWNER),
        DoctorCheck(id=STALE_CHECK_ID, run=_check_activation_stale, fix=None, owner=OWNER),
    ]
