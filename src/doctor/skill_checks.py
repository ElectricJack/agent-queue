"""``skills.*`` doctor checks — shipped aq-* skills vs. what is installed.

:func:`src.vault.ensure_default_aq_skills` copies ``src/skills/<name>/SKILL.md``
into every harness skill directory it finds, **write-if-absent**: an existing
installed copy is never overwritten, so an operator's edit survives an upgrade.

The cost of that rule is silent drift in the other direction: once a shipped
skill is corrected in-tree, every install that already has the old copy keeps
handing agents the stale guidance, and nothing says so.  This check is that
signal, and its fix is the sanctioned way back — it backs each drifted copy up
as ``SKILL.md.bak`` beside itself before re-copying the shipped version, so an
operator edit is recoverable rather than lost.
"""

from __future__ import annotations

import filecmp
import os
import shutil

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "vault"

CHECK_ID = "skills.installed_drift"


def _shipped_skills() -> dict[str, str]:
    """``{skill name: shipped SKILL.md path}`` for everything in ``src/skills``."""
    root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills")
    if not os.path.isdir(root):
        return {}
    out: dict[str, str] = {}
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name, "SKILL.md")
        if os.path.isfile(path):
            out[name] = path
    return out


def _installed_roots() -> list[str]:
    """Harness skill directories that exist on this box.

    Mirrors the target list in :func:`src.vault.ensure_default_aq_skills`;
    a directory that was never created is simply not an install to check.
    """
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".claude", "skills"),
        os.path.join(home, ".gemini", "skills"),
        os.path.join(home, "snap", "gemini-cli", "common", ".gemini", "skills"),
        os.path.join(home, ".codex", "skills"),
    ]
    return [path for path in candidates if os.path.isdir(path)]


def _drifted() -> list[tuple[str, str]]:
    """``(installed path, shipped path)`` for every copy that differs."""
    shipped = _shipped_skills()
    out: list[tuple[str, str]] = []
    for root in _installed_roots():
        for name, src_path in shipped.items():
            dst_path = os.path.join(root, name, "SKILL.md")
            if not os.path.isfile(dst_path):
                continue
            if not filecmp.cmp(src_path, dst_path, shallow=False):
                out.append((dst_path, src_path))
    return out


async def _check_installed_skill_drift(ctx: DoctorContext) -> CheckResult:
    shipped = _shipped_skills()
    if not shipped:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="no shipped skills found in src/skills",
        )
    roots = _installed_roots()
    if not roots:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="no harness skill directory on this box",
        )
    drifted = _drifted()
    if not drifted:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.OK,
            detail=f"{len(shipped)} shipped skill(s) match every installed copy",
            data={"skills": len(shipped), "roots": roots},
        )
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.WARN,
        fixable=True,
        detail=(
            f"{len(drifted)} installed SKILL.md file(s) differ from the shipped "
            "source (write-if-absent seeding never overwrites); run with --fix to "
            "back each up as SKILL.md.bak and re-copy"
        ),
        data={"drifted": [installed for installed, _ in drifted]},
    )


async def _fix_installed_skill_drift(ctx: DoctorContext) -> CheckResult:
    drifted = _drifted()
    repaired: list[str] = []
    failed: dict[str, str] = {}
    for installed, source in drifted:
        try:
            shutil.copy2(installed, installed + ".bak")
            shutil.copy2(source, installed)
            repaired.append(installed)
        except OSError as exc:  # unwritable install dir, full disk, …
            failed[installed] = str(exc)
    if failed:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.WARN,
            fixable=True,
            fix_applied=bool(repaired),
            detail=f"re-copied {len(repaired)}, failed {len(failed)}",
            data={"repaired": repaired, "failed": failed},
        )
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.OK,
        fixable=True,
        fix_applied=True,
        detail=f"re-copied {len(repaired)} skill file(s) from the shipped source",
        data={"repaired": repaired},
    )


def skill_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id=CHECK_ID,
            run=_check_installed_skill_drift,
            fix=_fix_installed_skill_drift,
            owner=OWNER,
        ),
    ]
