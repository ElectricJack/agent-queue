"""``skills.*`` doctor checks — shipped aq-* skills vs. what is installed.

:func:`src.vault.ensure_default_aq_skills` copies each ``src/skills/<name>/``
skill — ``SKILL.md`` plus its ``reference/*.md`` files — into every harness skill
directory it finds, **write-if-absent** per file: an existing installed copy is
never overwritten, so an operator's edit survives an upgrade.

The cost of that rule is silent drift in the other direction: once a shipped
skill is corrected in-tree, every install that already has the old copy keeps
handing agents the stale guidance, and nothing says so.  This check is that
signal, and its fix is the sanctioned way back — it backs each drifted copy up
as ``<file>.bak`` beside itself before re-copying the shipped version, so an
operator edit is recoverable rather than lost.  A reference file missing beside
an installed ``SKILL.md`` (a skill split after it was first installed) is
reported too, and the fix copies it in.
"""

from __future__ import annotations

import filecmp
import os
import shutil

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.vault import shipped_skill_files

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


def _skill_file_pairs() -> list[tuple[str, str]]:
    """``(installed path, shipped path)`` for every file of every installed skill.

    A skill whose ``SKILL.md`` is not installed under a root is skipped there:
    write-if-absent seeding will create the whole skill.
    """
    shipped = _shipped_skills()
    out: list[tuple[str, str]] = []
    for root in _installed_roots():
        for name, skill_md in shipped.items():
            if not os.path.isfile(os.path.join(root, name, "SKILL.md")):
                continue
            src_dir = os.path.dirname(skill_md)
            for rel in shipped_skill_files(src_dir):
                parts = rel.split("/")
                out.append((os.path.join(root, name, *parts), os.path.join(src_dir, *parts)))
    return out


def _drifted() -> list[tuple[str, str]]:
    """``(installed path, shipped path)`` for every installed copy that differs."""
    return [
        (dst, src)
        for dst, src in _skill_file_pairs()
        if os.path.isfile(dst) and not filecmp.cmp(src, dst, shallow=False)
    ]


def _missing() -> list[tuple[str, str]]:
    """``(installed path, shipped path)`` for reference files absent beside a SKILL.md."""
    return [(dst, src) for dst, src in _skill_file_pairs() if not os.path.exists(dst)]


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
    missing = _missing()
    if not drifted and not missing:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.OK,
            detail=f"{len(shipped)} shipped skill(s) match every installed copy",
            data={"skills": len(shipped), "roots": roots},
        )
    problems = []
    if drifted:
        problems.append(
            f"{len(drifted)} installed skill file(s) differ from the shipped source "
            "(write-if-absent seeding never overwrites)"
        )
    if missing:
        problems.append(f"{len(missing)} shipped reference file(s) are not installed")
    data: dict = {"drifted": [installed for installed, _ in drifted]}
    if missing:
        data["missing"] = [installed for installed, _ in missing]
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.WARN,
        fixable=True,
        detail="; ".join(problems) + "; run with --fix to back up (<file>.bak) and re-copy",
        data=data,
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
    for installed, source in _missing():
        try:
            os.makedirs(os.path.dirname(installed), exist_ok=True)
            shutil.copy2(source, installed)
            repaired.append(installed)
        except OSError as exc:
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
