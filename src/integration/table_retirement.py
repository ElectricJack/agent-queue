"""Retire legacy integration tables without losing their history.

rev-agile-ridge revision 2, §5.3, names the integration tables that stay and
37 legacy ones that are migrated into them or dropped; §6 phase 4 deletes the
legacy ones a family at a time, once nothing uses them.  This module is the
mechanism for that deletion and nothing else:

* :data:`FAMILIES` puts every legacy table in the family it retires with, and
  :data:`RETAINED_TABLES` names what stays and why;
* :func:`family_readiness` reads, and never writes, what still uses a family:
  source files that name a table, foreign keys from outside the family, rows;
* :func:`retire_families` is the destructive step.  Only the operator's
  ``aq db retire --apply`` calls it, with a fresh ``pg_dump -Fc`` backup.  In
  one transaction it locks the families, re-checks readiness, copies every
  row verbatim into ``integration_retired_rows``, records a receipt whose
  count and digest the archive must reproduce, and only then drops the
  tables.  Families whose tables reference each other (``repair`` and
  ``parent``) can only retire together;
* :func:`restore_retired_table` replays an archive into a table and checks the
  same count and digest, which proves the archive is a complete copy.

No migration calls :func:`retire_families`: neither an upgrade nor a cleanup
drops a legacy table on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.types import Text

#: A backup older than this does not hold the rows a retirement archives.
BACKUP_MAX_AGE_SECONDS = 24 * 60 * 60
#: The first bytes of a ``pg_dump --format=custom`` archive.
CUSTOM_DUMP_MAGIC = b"PGDMP"

_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_THIS_MODULE = Path(__file__).resolve()


@dataclass(frozen=True)
class RetirementFamily:
    """Legacy tables that retire together, and where §5.3 sends their data.

    ``code_removed`` is set by the change that deletes the family's last
    reader and writer; from then on ``tests/test_integration_table_retirement.py``
    refuses any source reference to its tables or any table left in metadata.
    """

    name: str
    target: str
    tables: tuple[str, ...]
    code_removed: bool = False


FAMILIES: tuple[RetirementFamily, ...] = (
    RetirementFamily(
        "root_train",
        "integration_subjects, integration_subject_members, integration_journal",
        (
            "integration_batches",
            "integration_batch_members",
            "integration_candidate_revisions",
            "integration_candidate_member_results",
            "integration_candidate_publications",
            "integration_candidate_resolutions",
            "integration_candidate_ref_mutations",
            "integration_root_intent_members",
            "integration_promotion_intents",
            "integration_root_authorizations",
        ),
    ),
    RetirementFamily(
        "repair",
        "integration_subjects, integration_writers, integration_journal",
        (
            "integration_repair_operations",
            "integration_repair_stages",
            "integration_repair_stage_evidence",
            "integration_delegate_releases",
        ),
    ),
    RetirementFamily(
        "parent",
        "integration_subjects, integration_subject_members, integration_journal",
        (
            "integration_parent_episodes",
            "integration_parent_verifications",
            "integration_parent_operation_completions",
            "integration_episode_receipt_acceptances",
            "integration_parent_verification_evidence",
            "integration_child_dispositions",
        ),
    ),
    RetirementFamily(
        "writers",
        "integration_writers, integration_subjects",
        (
            "integration_branch_owners",
            "integration_owner_recoveries",
            "project_integration_leases",
            "project_integration_schedules",
        ),
    ),
    RetirementFamily(
        "evidence",
        "integration_attempts, integration_journal",
        (
            "integration_check_evidence",
            "integration_attestation_publications",
            "integration_source_ci",
            "integration_release_results",
            "integration_cleanup_items",
        ),
    ),
    RetirementFamily(
        "legacy",
        "integration_journal (dropped once its counts are zero)",
        (
            "integration_legacy_gate_applicability",
            "integration_legacy_suppression",
            "integration_legacy_deliveries",
            "integration_history_waivers",
            "integration_history_waiver_consumptions",
            "integration_rollout_transitions",
        ),
    ),
    RetirementFamily(
        "artifact_pins",
        "integration_subjects.policy_artifact_sha256, integration_journal",
        ("integration_operation_artifact_pins", "integration_outbox_artifact_pins"),
    ),
)

#: Integration tables no family retires.  The last two are absent from §5.3's
#: lists; they stay until the schema owner classifies them.
RETAINED_TABLES: dict[str, str] = {
    "integration_subjects": "§5.3 target",
    "integration_subject_journal": "§5.3 target (integration_journal)",
    "integration_outbox": "§5.3 target, short retention",
    "task_branch_origins": "§5.3 retained task-side table",
    "task_delivery_receipts": "§5.3 retained task-side table",
    "integration_table_retirements": "retirement receipts",
    "integration_retired_rows": "retirement archive",
    "task_integration_checkpoints": "not named by §5.3; unclassified",
    "integration_review_evidence": "not named by §5.3; unclassified",
}


class RetirementRefused(Exception):
    """A retirement or restore check failed; nothing was changed."""

    def __init__(self, reasons: list[str] | tuple[str, ...]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class BackupEvidence:
    path: str
    sha256: str
    size: int
    modified_at: float


@dataclass(frozen=True)
class TableReadiness:
    table: str
    family: str
    present: bool
    retired: bool
    rows: int
    code_references: tuple[str, ...]
    referenced_by: tuple[str, ...]

    @property
    def blockers(self) -> tuple[str, ...]:
        found = []
        if self.code_references:
            found.append(
                f"{self.table}: named by {len(self.code_references)} source file(s), "
                f"e.g. {self.code_references[0]}"
            )
        if self.referenced_by:
            found.append(
                f"{self.table}: foreign keys from outside the family: "
                + ", ".join(self.referenced_by)
            )
        if self.retired and self.present:
            found.append(f"{self.table}: present again after its retirement receipt")
        return tuple(found)


def family(name: str) -> RetirementFamily:
    for candidate in FAMILIES:
        if candidate.name == name:
            return candidate
    raise KeyError(name)


def _quoted(table: str) -> str:
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError(f"not a plain table name: {table!r}")
    return f'"{table}"'


def code_references(
    tables: tuple[str, ...], *, source_root: Path | None = None
) -> dict[str, tuple[str, ...]]:
    """The Python source files under *source_root* that name each table.

    Any whole-word mention counts, including ``tables.py`` and docstrings: a
    table is not retired while the code still defines or describes it.  This
    module is skipped because it is the registry.
    """
    root = (source_root or _SOURCE_ROOT).resolve()
    if not tables:
        return {}
    found: dict[str, list[str]] = {table: [] for table in tables}
    # One pass per file: a whole word is one name, so the alternation's order
    # never hides a table behind another.
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, tables)) + r")\b")
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == _THIS_MODULE:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        for table in sorted(set(pattern.findall(source))):
            found[table].append(str(path.relative_to(root.parent)))
    return {table: tuple(paths) for table, paths in found.items()}


def verify_backup(
    path: str | Path,
    *,
    max_age_seconds: float = BACKUP_MAX_AGE_SECONDS,
    now: float | None = None,
) -> BackupEvidence:
    """Check that *path* is a recent ``pg_dump --format=custom`` archive."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise RetirementRefused([f"backup {file} does not exist"])
    stat = file.stat()
    if stat.st_size == 0:
        raise RetirementRefused([f"backup {file} is empty"])
    digest = hashlib.sha256()
    with file.open("rb") as handle:
        if handle.read(len(CUSTOM_DUMP_MAGIC)) != CUSTOM_DUMP_MAGIC:
            raise RetirementRefused([f"backup {file} is not a pg_dump --format=custom archive"])
        handle.seek(0)
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    age = (time.time() if now is None else now) - stat.st_mtime
    if age > max_age_seconds:
        raise RetirementRefused(
            [f"backup {file} is {age / 3600:.1f}h old; take one within {max_age_seconds / 3600:g}h"]
        )
    return BackupEvidence(str(file.resolve()), digest.hexdigest(), stat.st_size, stat.st_mtime)


def _names(statement: str):
    return text(statement).bindparams(bindparam("names", type_=ARRAY(Text)))


async def _present(conn: AsyncConnection, tables: tuple[str, ...]) -> set[str]:
    result = await conn.execute(
        _names(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p') AND n.nspname = current_schema() "
            "AND c.relname = ANY(:names)"
        ),
        {"names": list(tables)},
    )
    return {row[0] for row in result}


async def _receipts(conn: AsyncConnection, tables: tuple[str, ...]) -> set[str]:
    if not await conn.scalar(text("SELECT to_regclass('integration_table_retirements')")):
        return set()
    result = await conn.execute(
        _names(
            "SELECT table_name FROM integration_table_retirements WHERE table_name = ANY(:names)"
        ),
        {"names": list(tables)},
    )
    return {row[0] for row in result}


async def _referencing(conn: AsyncConnection, tables: tuple[str, ...]) -> dict[str, set[str]]:
    result = await conn.execute(
        _names(
            "SELECT dst.relname, src.relname FROM pg_constraint c "
            "JOIN pg_class src ON src.oid = c.conrelid "
            "JOIN pg_class dst ON dst.oid = c.confrelid "
            "JOIN pg_namespace n ON n.oid = dst.relnamespace "
            "WHERE c.contype = 'f' AND n.nspname = current_schema() "
            "AND dst.relname = ANY(:names)"
        ),
        {"names": list(tables)},
    )
    found: dict[str, set[str]] = {}
    for referenced, referencing in result:
        found.setdefault(referenced, set()).add(referencing)
    return found


async def family_readiness(
    conn: AsyncConnection,
    retiring: RetirementFamily,
    *,
    alongside: Sequence[RetirementFamily] = (),
    source_root: Path | None = None,
) -> list[TableReadiness]:
    """What still uses each of *retiring*'s tables.  Read-only.

    A foreign key from a table of a family in *alongside* is not a blocker:
    families that reference each other retire in one transaction.
    """
    tables = retiring.tables
    together = set(tables).union(*(other.tables for other in alongside))
    present = await _present(conn, tables)
    receipts = await _receipts(conn, tables)
    referencing = await _referencing(conn, tables)
    references = code_references(tables, source_root=source_root)
    report = []
    for table in tables:
        rows = 0
        if table in present:
            rows = int(await conn.scalar(text(f"SELECT count(*) FROM {_quoted(table)}")))
        outside = sorted(referencing.get(table, set()) - together)
        report.append(
            TableReadiness(
                table=table,
                family=retiring.name,
                present=table in present,
                retired=table in receipts,
                rows=rows,
                code_references=references[table],
                referenced_by=tuple(outside),
            )
        )
    return report


def _digest_sql(source: str) -> str:
    """Count and order-independent digest of the JSONB rows *source* selects."""
    return (
        "SELECT count(*), 'sha256:' || encode(sha256(convert_to(coalesce("
        "string_agg(r::text, E'\\n' ORDER BY r::text), ''), 'UTF8')), 'hex') "
        f"FROM ({source}) AS s(r)"
    )


async def _table_digest(conn: AsyncConnection, table: str) -> tuple[int, str]:
    row = (
        await conn.execute(text(_digest_sql(f"SELECT to_jsonb(t) FROM {_quoted(table)} t")))
    ).one()
    return int(row[0]), row[1]


async def _archive_digest(conn: AsyncConnection, table: str) -> tuple[int, str]:
    row = (
        await conn.execute(
            text(
                _digest_sql(
                    "SELECT row_data FROM integration_retired_rows WHERE table_name = :table"
                )
            ),
            {"table": table},
        )
    ).one()
    return int(row[0]), row[1]


async def _columns(conn: AsyncConnection, table: str) -> list[dict]:
    result = await conn.execute(
        text(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull "
            "FROM pg_attribute a WHERE a.attrelid = CAST(:table AS regclass) "
            "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
        ),
        {"table": table},
    )
    return [
        {"name": name, "type": kind, "not_null": bool(not_null)} for name, kind, not_null in result
    ]


async def _schema_revision(conn: AsyncConnection) -> str:
    result = await conn.execute(text("SELECT version_num FROM alembic_version"))
    return ",".join(sorted(row[0] for row in result)) or "unstamped"


async def _require_archive(conn: AsyncConnection) -> None:
    missing = [
        table
        for table in ("integration_table_retirements", "integration_retired_rows")
        if not await conn.scalar(text(f"SELECT to_regclass('{table}')"))
    ]
    if missing:
        raise RetirementRefused(
            [f"{', '.join(missing)} missing: run `aq db upgrade` to a00000000064 or later"]
        )


#: ``to_jsonb`` renders timestamps, bytea, floats and intervals through these
#: settings, so retire and restore pin them or one table digests two ways.
_DIGEST_SETTINGS = (
    ("TimeZone", "UTC"),
    ("bytea_output", "hex"),
    ("extra_float_digits", "1"),
    ("IntervalStyle", "iso_8601"),
)


async def _pin_session(conn: AsyncConnection) -> None:
    for name, value in _DIGEST_SETTINGS:
        await conn.execute(
            text("SELECT set_config(:name, :value, true)"), {"name": name, "value": value}
        )


async def _lock(conn: AsyncConnection, tables: list[str]) -> None:
    try:
        await conn.execute(
            text(
                "LOCK TABLE "
                + ", ".join(_quoted(table) for table in tables)
                + " IN ACCESS EXCLUSIVE MODE NOWAIT"
            )
        )
    except DBAPIError as exc:
        raise RetirementRefused([f"a family table is in use: {exc.orig}"]) from exc


async def retire_families(
    conn: AsyncConnection,
    families: Sequence[RetirementFamily],
    *,
    backup: BackupEvidence,
    retired_by: str,
    source_root: Path | None = None,
    now: float | None = None,
) -> list[dict]:
    """Archive and drop *families*' tables inside the caller's transaction.

    Refuses, changing nothing, while any table has a blocker.  A table that is
    already gone is skipped: either an earlier run retired it (its receipt
    stands) or a fresh install never created it.  Returns the new receipts.
    """
    if not families:
        raise RetirementRefused(["name at least one family"])
    await _require_archive(conn)
    await _pin_session(conn)
    owner = {table: retiring.name for retiring in families for table in retiring.tables}
    present = sorted(await _present(conn, tuple(owner)))
    if present:
        await _lock(conn, present)
    blockers = [
        reason
        for retiring in families
        for readiness in await family_readiness(
            conn, retiring, alongside=families, source_root=source_root
        )
        for reason in readiness.blockers
    ]
    if blockers:
        raise RetirementRefused(blockers)
    if not present:
        return []
    revision = await _schema_revision(conn)
    retired_at = time.time() if now is None else now
    receipts = []
    for table in present:
        row_count, rows_digest = await _table_digest(conn, table)
        receipt = {
            "table_name": table,
            "family": owner[table],
            "row_count": row_count,
            "rows_digest": rows_digest,
            "columns": await _columns(conn, table),
            "backup_path": backup.path,
            "backup_sha256": backup.sha256,
            "schema_revision": revision,
            "retired_by": retired_by,
            "retired_at": retired_at,
        }
        await conn.execute(
            text(
                "INSERT INTO integration_table_retirements (table_name, family, row_count, "
                "rows_digest, columns, backup_path, backup_sha256, schema_revision, "
                "retired_by, retired_at) VALUES (:table_name, :family, :row_count, "
                ":rows_digest, CAST(:columns AS jsonb), :backup_path, :backup_sha256, "
                ":schema_revision, :retired_by, :retired_at)"
            ),
            {**receipt, "columns": json.dumps(receipt["columns"], sort_keys=True)},
        )
        await conn.execute(
            text(
                "INSERT INTO integration_retired_rows (table_name, ordinal, row_data) "
                "SELECT :table, row_number() OVER (ORDER BY r::text), r "
                f"FROM (SELECT to_jsonb(t) FROM {_quoted(table)} t) AS s(r)"
            ),
            {"table": table},
        )
        if await _archive_digest(conn, table) != (row_count, rows_digest):
            raise RetirementRefused([f"{table}: archive does not reproduce the table's digest"])
        receipts.append(receipt)
    # One statement: foreign keys among the retiring tables never order the
    # drop, and anything else that still depends on one refuses it.
    await conn.execute(text("DROP TABLE " + ", ".join(_quoted(table) for table in present)))
    return receipts


async def restore_retired_table(conn: AsyncConnection, table: str) -> dict:
    """Replay *table*'s archive and prove it matches the retirement receipt.

    An absent table is recreated bare (columns, types and NOT NULL only, so
    constraints, defaults and indexes come from the backup when they are
    needed); a table that exists must be empty.
    """
    await _require_archive(conn)
    await _pin_session(conn)
    receipt = (
        await conn.execute(
            text(
                "SELECT row_count, rows_digest, columns FROM integration_table_retirements "
                "WHERE table_name = :table"
            ),
            {"table": table},
        )
    ).one_or_none()
    if receipt is None:
        raise RetirementRefused([f"{table} has no retirement receipt"])
    row_count, rows_digest, columns = receipt
    created = not await _present(conn, (table,))
    if created:
        definition = ", ".join(
            f"{_quoted(column['name'])} {column['type']}"
            + (" NOT NULL" if column["not_null"] else "")
            for column in columns
        )
        await conn.execute(text(f"CREATE TABLE {_quoted(table)} ({definition})"))
    elif await conn.scalar(text(f"SELECT EXISTS (SELECT 1 FROM {_quoted(table)})")):
        raise RetirementRefused([f"{table} exists and is not empty"])
    await conn.execute(
        text(
            f"INSERT INTO {_quoted(table)} OVERRIDING SYSTEM VALUE "
            f"SELECT (jsonb_populate_record(NULL::{_quoted(table)}, row_data)).* "
            "FROM integration_retired_rows WHERE table_name = :table ORDER BY ordinal"
        ),
        {"table": table},
    )
    if await _table_digest(conn, table) != (row_count, rows_digest):
        raise RetirementRefused([f"{table}: restored rows do not reproduce the receipt's digest"])
    return {"table": table, "rows": row_count, "rows_digest": rows_digest, "created": created}
