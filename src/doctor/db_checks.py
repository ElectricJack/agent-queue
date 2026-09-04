"""``db.alembic_orphan`` — the repair path for the 2026-09-02 incident.

Symptom: the daemon will not start, with ``Alembic preflight failed:
alembic_version references unknown revision(s) ['f2a4c6e8b0d2']``.  Cause: a
process outside the daemon migrated the production database with a branch's
migration, that branch is not merged, and the revision file therefore does not
exist in this checkout.  Alembic cannot walk a chain whose head it has never
seen, so nothing moves until someone reconciles the row.

:mod:`src.database.migration_guard` stops that from happening again.  This
check is for the databases where it already happened.  It answers the two
questions the operator actually has:

* **Where did this revision come from?**  Every ``origin/*`` ref (and every
  local branch) is searched for the file that declares it, so the answer is
  "``aq/bold-dune-47``, ``migrations/versions/f2a4c6e8b0d2_....py``" rather
  than a hex string with no provenance.
* **How do I get back?**  When the file is found, ``--fix`` materialises it
  into a private temporary directory that Alembic reads *alongside*
  ``migrations/versions/`` (``version_locations``) just long enough to run
  *its own* ``downgrade()``, leaving the database stamped at its parent — a
  revision this checkout does have.  The checkout is never written to.  That
  is the only repair that undoes the schema change rather than lying about
  it.

The stamp fallback (for an orphan whose file cannot be found anywhere) is
deliberately *not* reachable from ``--fix`` alone: rewriting
``alembic_version`` without running a downgrade leaves whatever DDL the
orphan applied in place, which is safe only when the change was additive.
Nobody can know that from the outside, so it requires
``AQ_DOCTOR_ALEMBIC_STAMP=1`` on top of ``--fix`` — two deliberate acts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

logger = logging.getLogger(__name__)

OWNER = "core"

CHECK_ID = "db.alembic_orphan"

#: Opt-in for the stamp fallback.  See the module docstring.
STAMP_ENV = "AQ_DOCTOR_ALEMBIC_STAMP"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_VERSIONS_DIR = _PROJECT_ROOT / "migrations" / "versions"

#: Enough refs to cover "some branch on origin", not so many that a busy
#: fork turns one doctor check into a minute of ``git grep``.
_MAX_REFS = 200


# ---------------------------------------------------------------------------
# Reading the two sides
# ---------------------------------------------------------------------------


def _known_revisions() -> tuple[set[str], list[str]]:
    """``(every revision in this checkout, the head(s))``."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(_PROJECT_ROOT / "alembic.ini")))
    return {s.revision for s in script.walk_revisions()}, sorted(script.get_heads())


async def _stamped_revisions(ctx: DoctorContext) -> list[str] | None:
    """The database's ``alembic_version`` rows, or ``None`` if unreadable."""
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    engine = getattr(ctx.db, "_engine", None) if ctx.db is not None else None
    if engine is None:
        return None
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            return sorted(row[0] for row in result.fetchall())
    except SQLAlchemyError:
        return None


# ---------------------------------------------------------------------------
# Provenance: which branch defines this revision?
# ---------------------------------------------------------------------------


async def _git(*args: str, timeout: float = 10.0) -> str:
    """Run a read-only git command in the checkout; ``""`` on any failure.

    Spawned off the loop rather than through ``subprocess.run`` — doctor
    checks run concurrently and the repo convention is async-only git.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(_PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.debug("git %s could not start: %s", args, exc)
        return ""
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.debug("git %s timed out after %ss", args, timeout)
        return ""
    if proc.returncode != 0:
        return ""
    return stdout.decode("utf-8", errors="replace")


async def _candidate_refs() -> list[str]:
    """``origin/*`` first — the incident's revisions came from unmerged PRs."""
    out = await _git(
        "for-each-ref",
        "--format=%(refname)",
        "--sort=-committerdate",
        "refs/remotes",
        "refs/heads",
    )
    return [line.strip() for line in out.splitlines() if line.strip()][:_MAX_REFS]


#: Matches the *declaration* of a revision id, not a mention of it.  A merge
#: revision names its parents in ``down_revision = ("a", "b")``, so a plain
#: substring search happily reports the wrong file — which is exactly what a
#: repo that has already merged past the orphan looks like.
_DECLARATION_PATTERN = "^revision(: *str)? *= *[\"']{revision}[\"']"


async def find_revision_source(revision: str) -> tuple[str, str] | None:
    """``(ref, path)`` of the migration file declaring *revision*, or None.

    Matches the ``revision = "<id>"`` line every Alembic template writes, so
    a merge revision that merely lists *revision* among its parents is not
    mistaken for its definition.
    """
    if not revision or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", revision):
        # ``alembic_version`` is a plain string column; never build a regex
        # out of whatever happens to be in it.
        return None
    refs = await _candidate_refs()
    if not refs:
        return None
    # One ``git grep`` over every ref beats one subprocess per ref on a repo
    # with a few hundred branches, and git reports ``<ref>:<path>`` per hit.
    out = await _git(
        "grep",
        "-l",
        "-E",
        _DECLARATION_PATTERN.format(revision=revision),
        *refs,
        "--",
        "migrations/versions/*.py",
        timeout=60.0,
    )
    for line in out.splitlines():
        ref, _, path = line.partition(":")
        if path:
            return ref, path
    return None


async def _revision_file_text(ref: str, path: str) -> str:
    return await _git("show", f"{ref}:{path}", timeout=15.0)


def _parent_revision(source: str) -> str | None:
    """The ``down_revision`` declared in a revision file's source.

    Parsed with :mod:`ast`, never executed: the file comes from an arbitrary
    branch, and this runs on the operator's machine.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in tree.body:
        targets = [node.target] if isinstance(node, ast.AnnAssign) else getattr(node, "targets", [])
        if not any(isinstance(t, ast.Name) and t.id == "down_revision" for t in targets):
            continue
        try:
            parent = ast.literal_eval(node.value) if node.value is not None else None
        except ValueError:
            return None
        return parent if isinstance(parent, str) else None
    return None


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


async def _check_alembic_orphan(ctx: DoctorContext) -> CheckResult:
    stamped = await _stamped_revisions(ctx)
    if stamped is None:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="alembic_version unreadable — database not initialised",
        )
    try:
        known, heads = await asyncio.to_thread(_known_revisions)
    except Exception as exc:  # noqa: BLE001 - a broken script dir is the finding
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.ERROR,
            detail=f"could not read alembic script directory: {type(exc).__name__}: {exc}",
        )

    orphans = [rev for rev in stamped if rev not in known]
    if not orphans:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.OK,
            detail=f"every stamped revision is known to this checkout ({', '.join(stamped)})",
            data={"stamped": stamped, "heads": heads},
        )

    sources = {rev: await find_revision_source(rev) for rev in orphans}
    parts: list[str] = []
    for rev in orphans:
        found = sources[rev]
        if found:
            ref, path = found
            parts.append(f"{rev} (defined on {ref} in {path})")
        else:
            parts.append(f"{rev} (not found on any local or remote ref)")
    repairable = any(sources[rev] for rev in orphans)
    hint = (
        "re-run with --fix to run that revision's own downgrade() and leave the "
        "database at its parent"
        if repairable
        else f"no file found; set {STAMP_ENV}=1 with --fix to stamp back to "
        f"{', '.join(heads)} instead (only correct if the orphan's change was additive)"
    )
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.ERROR,
        detail=(
            f"alembic_version references {len(orphans)} revision(s) this checkout does not "
            f"have: {'; '.join(parts)} — the daemon will refuse to start. " + hint
        ),
        fixable=True,
        data={
            "stamped": stamped,
            "heads": heads,
            "orphans": orphans,
            "sources": {rev: list(found) if found else None for rev, found in sources.items()},
        },
    )


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------


def _downgrade_with_borrowed_file(ctx_db_url: str, revision: str, source: str) -> str:
    """Run *revision*'s own ``downgrade()`` by borrowing its file from *ref*.

    Alembic can only walk a chain whose files it can see, so the orphan's
    revision file has to be somewhere Alembic enumerates.  It is written to
    a private temporary directory and Alembic is pointed at *both* that
    directory and the checkout's ``migrations/versions`` through
    ``version_locations`` — never into the checkout itself.  Writing it into
    the real ``versions/`` (and unlinking it afterwards) mutates state every
    concurrent Alembic scan reads: another process that lists the directory
    during that window sees the file appear and then vanish before it is
    loaded and fails with ``Can't find Python file``.  That is what made the
    migration-marked CI arm flaky under pytest-xdist, and on an operator's
    machine it would have been a second ``alembic`` shell.  The temp dir is
    removed afterwards — including when the downgrade raises.
    """
    import tempfile

    from alembic import command

    parent = _parent_revision(source)
    if not parent:
        raise RuntimeError(f"revision {revision} declares no single down_revision to fall back to")

    with tempfile.TemporaryDirectory(prefix="aq-doctor-orphan-") as borrowed_dir:
        (Path(borrowed_dir) / f"_orphan_{revision}.py").write_text(source, encoding="utf-8")
        cfg = _alembic_config(ctx_db_url, extra_version_locations=[Path(borrowed_dir)])
        command.downgrade(cfg, parent)
    return parent


def _alembic_config(ctx_db_url: str, *, extra_version_locations: list[Path] = ()):
    """The checkout's ``alembic.ini`` bound to *ctx_db_url*.

    With *extra_version_locations*, Alembic also enumerates those directories
    for revision files.  Setting ``version_locations`` at all replaces the
    implicit ``<script_location>/versions``, so that directory is always
    listed first.  Values go through ``ConfigParser`` interpolation, hence the
    ``%`` escaping — a temp dir under an unusual ``TMPDIR`` must not turn into
    an interpolation error.
    """
    from alembic.config import Config

    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", ctx_db_url)
    if extra_version_locations:
        locations = [_VERSIONS_DIR, *extra_version_locations]
        cfg.set_main_option(
            "version_locations",
            os.pathsep.join(str(loc).replace("%", "%%") for loc in locations),
        )
    return cfg


def _stamp(ctx_db_url: str, target: str) -> None:
    """Rewrite ``alembic_version`` to *target*, ignoring what it says now.

    ``purge=True`` is not optional here: a plain stamp asks Alembic to walk
    from the current revision to the target, and the current revision is
    precisely the one it cannot locate.  Purging drops the row first, which is
    the only thing that works — and the reason this path needs its own opt-in
    (see the module docstring), since it asserts a schema state rather than
    reaching one.
    """
    from alembic import command

    command.stamp(_alembic_config(ctx_db_url), target, purge=True)


def _alembic_url(ctx: DoctorContext) -> str:
    """The daemon's database as a DSN ``migrations/env.py`` can drive.

    That env builds an *async* engine unconditionally, so the driver has to
    be spelled out — a bare ``sqlite:///`` DSN fails with "the loaded
    'pysqlite' is not async".
    """
    url = ctx.config.database.url or ctx.config.database_path
    if getattr(ctx.config.database, "backend", "sqlite") == "postgresql":
        if "+" in url.split("://", 1)[0]:
            return url
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return f"sqlite+aiosqlite:///{os.path.expanduser(url)}"


async def _fix_alembic_orphan(ctx: DoctorContext) -> CheckResult:
    """Repair one orphaned ``alembic_version`` row.  Never runs unattended."""
    before = await _check_alembic_orphan(ctx)
    orphans: list[str] = before.data.get("orphans", [])
    if not orphans:
        return before

    url = _alembic_url(ctx)
    repaired: list[str] = []
    for revision in orphans:
        found = await find_revision_source(revision)
        if found:
            ref, path = found
            source = await _revision_file_text(ref, path)
            if not source:
                raise RuntimeError(f"could not read {path} from {ref}")
            parent = await asyncio.to_thread(_downgrade_with_borrowed_file, url, revision, source)
            repaired.append(f"{revision} -> {parent} (downgraded from {ref})")
            continue
        if os.environ.get(STAMP_ENV) != "1":
            raise RuntimeError(
                f"{revision} has no revision file on any ref; stamping past it would "
                f"leave its schema change in place. Set {STAMP_ENV}=1 to stamp back to "
                "this checkout's head anyway."
            )
        target = (before.data.get("heads") or ["head"])[0]
        await asyncio.to_thread(_stamp, url, target)
        repaired.append(f"{revision} -> {target} (stamped)")

    logger.warning("db.alembic_orphan repaired: %s", "; ".join(repaired))
    return before


def db_checks() -> list[DoctorCheck]:
    """Database-integrity checks doctor registers alongside the built-ins."""
    return [
        DoctorCheck(
            id=CHECK_ID,
            run=_check_alembic_orphan,
            fix=_fix_alembic_orphan,
            timeout_s=90.0,
            owner=OWNER,
        )
    ]
