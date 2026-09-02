"""capability.* doctor checks (Playbook V2 Package 0 §10, T-19).

Three operator signals, mirroring ``src/doctor/pool_checks.py``'s shape:

- ``capability.enforcement`` — **warn** while ``security.capability_enforcement``
  is not ``enforce``.  The shipped default is ``audit`` so an un-migrated fleet
  keeps running; the warning is what stops that becoming permanent.
- ``capability.legacy_profiles`` — **warn** per profile still on the legacy
  ``allowed_tools`` shape.  This is exactly the migration list Package 6 must
  clear before flipping the flag, and the same list ``aq profile audit``
  prints.
- ``capability.wildcards`` — **fail** when any stored profile contains a
  wildcard.  Parse and sync both reject one now, so a stored wildcard means a
  row predating this package or written around the sync path; it is the one
  state that makes a policy unconstructible at read time.

All three are report-only: none has a ``fix``.  Flipping the enforcement mode
and rewriting a profile's capabilities are operator decisions, and a stored
wildcard needs a human to decide which names were meant.
"""

from __future__ import annotations

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.profiles.capabilities import NAMESPACES, WILDCARD_CHARS

OWNER = "playbook-v2-package-0"


def _no_db_result(check_id: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        severity=Severity.INFO,
        detail="database not initialised — profile capabilities unknown",
    )


def _is_legacy(profile) -> bool:
    """True when no capability namespace was authored on *profile*."""
    return all(getattr(profile, ns, None) is None for ns in NAMESPACES)


def _wildcard_entries(profile) -> list[str]:
    names: list[str] = []
    for field_name in ("allowed_tools", *NAMESPACES):
        for name in getattr(profile, field_name, None) or []:
            if isinstance(name, str) and any(ch in name for ch in WILDCARD_CHARS):
                names.append(f"{field_name}:{name}")
    return sorted(set(names))


async def _check_enforcement(ctx: DoctorContext) -> CheckResult:
    mode = getattr(getattr(ctx.config, "security", None), "capability_enforcement", None)
    if not isinstance(mode, str):
        return CheckResult(
            id="capability.enforcement",
            severity=Severity.INFO,
            detail="no security config loaded — enforcement mode unknown",
        )
    if mode == "enforce":
        return CheckResult(
            id="capability.enforcement",
            severity=Severity.OK,
            detail="capability enforcement is on",
            data={"mode": mode},
        )
    return CheckResult(
        id="capability.enforcement",
        severity=Severity.WARN,
        detail=(
            f"security.capability_enforcement is '{mode}', not 'enforce' — "
            "legacy-shaped and unresolved principals are warned about rather "
            "than denied. Migrate profiles to ## Capabilities "
            "(`aq profile audit`), then set it to 'enforce'."
        ),
        data={"mode": mode},
    )


async def _check_legacy_profiles(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("capability.legacy_profiles")
    legacy = sorted(p.id for p in await ctx.db.list_profiles() if _is_legacy(p))
    if not legacy:
        return CheckResult(
            id="capability.legacy_profiles",
            severity=Severity.OK,
            detail="every profile declares explicit capabilities",
        )
    return CheckResult(
        id="capability.legacy_profiles",
        severity=Severity.WARN,
        detail=(
            f"{len(legacy)} profile(s) still derive capabilities from "
            f"allowed_tools: {', '.join(legacy[:10])}"
            f"{' …' if len(legacy) > 10 else ''}. "
            "Add a ## Capabilities block to each before enabling enforcement."
        ),
        data={"count": len(legacy), "profiles": legacy[:50]},
    )


async def _check_wildcards(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return _no_db_result("capability.wildcards")
    offenders = {}
    for profile in await ctx.db.list_profiles():
        entries = _wildcard_entries(profile)
        if entries:
            offenders[profile.id] = entries
    if not offenders:
        return CheckResult(
            id="capability.wildcards",
            severity=Severity.OK,
            detail="no stored profile contains a wildcard capability",
        )
    return CheckResult(
        id="capability.wildcards",
        severity=Severity.ERROR,
        detail=(
            f"{len(offenders)} profile(s) contain wildcard capabilities, which are "
            f"prohibited: {', '.join(sorted(offenders))}. A policy cannot be built "
            "from them, so those sessions fail closed. List every name explicitly."
        ),
        data={"count": len(offenders), "profiles": offenders},
    )


def capability_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id="capability.enforcement", run=_check_enforcement, owner=OWNER),
        DoctorCheck(id="capability.legacy_profiles", run=_check_legacy_profiles, owner=OWNER),
        DoctorCheck(id="capability.wildcards", run=_check_wildcards, owner=OWNER),
    ]


#: Snapshot for call-sites that want the list without a full registry.
CHECKS = capability_checks()

__all__ = ["CHECKS", "OWNER", "capability_checks"]
