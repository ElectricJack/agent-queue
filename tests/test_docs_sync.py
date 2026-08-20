"""Docs-sync invariants — catch drift between code and the specs.

Cheap tests, no tooling beyond pytest (design ``trust-and-ops`` §6).  The one
implemented here compares the SQLAlchemy metadata against the table catalog in
``docs/specs/database.md``.  When it fails, the message says exactly which side
to update — the doc is a spec, so "specs first, then code" means a schema change
lands with its doc row in the same commit.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.database.tables import metadata

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DB_SPEC = _REPO_ROOT / "docs" / "specs" / "database.md"

#: Tables that exist in the database but are deliberately not documented as
#: part of the application schema.  Keep this list short and explicit — every
#: entry is a decision, not an oversight.
UNDOCUMENTED_TABLES: frozenset[str] = frozenset(
    {
        "alembic_version",  # Alembic's own bookkeeping, not application schema
    }
)

#: Heading form the doc uses for each table section.
_TABLE_HEADING_RE = re.compile(r"^#{2,4}\s+Table:\s+`([a-z_][a-z0-9_]*)`", re.MULTILINE)


def _documented_tables() -> set[str]:
    text = _DB_SPEC.read_text(encoding="utf-8")
    return set(_TABLE_HEADING_RE.findall(text))


def _code_tables() -> set[str]:
    return set(metadata.tables) - UNDOCUMENTED_TABLES


class TestDatabaseSpecSync:
    def test_spec_file_exists(self):
        assert _DB_SPEC.is_file(), f"missing schema spec: {_DB_SPEC}"

    def test_every_table_in_code_is_documented(self):
        missing = sorted(_code_tables() - _documented_tables())
        assert not missing, (
            f"{len(missing)} table(s) defined in src/database/tables.py have no "
            f"'### Table: `<name>`' section in docs/specs/database.md: "
            f"{', '.join(missing)}. Add the section (or add the name to "
            "UNDOCUMENTED_TABLES with a reason)."
        )

    def test_every_documented_table_exists_in_code(self):
        stale = sorted(_documented_tables() - _code_tables() - UNDOCUMENTED_TABLES)
        assert not stale, (
            f"{len(stale)} table(s) documented in docs/specs/database.md no longer "
            f"exist in src/database/tables.py: {', '.join(stale)}. Remove the "
            "section (or restore the table)."
        )

    def test_doc_and_code_agree_exactly(self):
        assert _documented_tables() == _code_tables()

    @pytest.mark.parametrize("name", sorted(UNDOCUMENTED_TABLES))
    def test_exclusions_are_not_application_tables(self, name):
        """An exclusion must be justified — it must not be a table we define."""
        assert name not in metadata.tables, (
            f"{name!r} is defined in tables.py, so it should be documented "
            "rather than excluded."
        )

    def test_documented_column_names_match_code(self):
        """Each table section's backticked column names must exist in the table.

        Only the forward direction is enforced: a doc row naming a column that
        no longer exists is drift.  Columns present in code but absent from the
        doc are caught by review, not here — prose tables legitimately omit
        nothing today, but enforcing both directions would make the test brittle
        against formatting.
        """
        text = _DB_SPEC.read_text(encoding="utf-8")
        sections = re.split(r"^#{2,4}\s+Table:\s+`([a-z_][a-z0-9_]*)`", text, flags=re.MULTILINE)
        # sections = [preamble, name1, body1, name2, body2, ...]
        problems: list[str] = []
        for i in range(1, len(sections) - 1, 2):
            name, body = sections[i], sections[i + 1]
            table = metadata.tables.get(name)
            if table is None:
                continue
            actual = {c.name for c in table.columns}
            for line in body.splitlines():
                if not line.startswith("| `"):
                    continue
                match = re.match(r"^\|\s+`([a-z_][a-z0-9_]*)`", line)
                if match and match.group(1) not in actual:
                    problems.append(f"{name}.{match.group(1)}")
        assert not problems, (
            "docs/specs/database.md documents columns that no longer exist: "
            + ", ".join(sorted(problems))
        )
