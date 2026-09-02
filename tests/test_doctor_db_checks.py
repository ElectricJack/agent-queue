"""``db.alembic_orphan`` — the repair path for a poisoned ``alembic_version``.

The shape being tested is the 2026-09-02 incident's aftermath: production is
stamped with a revision that exists only on an unmerged branch, so Alembic
cannot walk the chain and the daemon will not start.  The check has to name
*where* the revision comes from, and ``--fix`` has to undo it rather than lie
about it.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest

from src.config import AppConfig, DatabaseConfig
from src.database import Database
from src.doctor.db_checks import (
    CHECK_ID,
    STAMP_ENV,
    _parent_revision,
    db_checks,
    find_revision_source,
)
from src.doctor.models import DoctorContext, Severity
from src.doctor.runner import apply_fix

#: ``src.doctor`` re-exports a *function* named ``db_checks``, which shadows
#: the submodule of the same name for a plain ``import`` — same convention as
#: ``pool_checks``.  Reach the module explicitly to monkeypatch inside it.
db_checks_module = importlib.import_module("src.doctor.db_checks")

#: The revision that actually broke the daemon on 2026-09-02.  It has since
#: been merged, so this repo *can* find its file — which makes it the right
#: fixture for the "traced to a branch" case.
REAL_ORPHAN = "f2a4c6e8b0d2"

#: A revision no ref declares, for the "cannot be traced" case.  Invented on
#: purpose: asserting on a real id would make the test depend on which
#: branches happen to be checked out.
ORPHAN = "0badc0ffee00"


@pytest.fixture
async def ctx(tmp_path):
    """A doctor context over a real, fully migrated SQLite database."""
    db_path = tmp_path / "aq.db"
    db = Database(str(db_path))
    await db.initialize()
    config = AppConfig(data_dir=str(tmp_path), database=DatabaseConfig(url=str(db_path)))
    try:
        yield DoctorContext(config=config, db=db, handler=None)
    finally:
        await db.close()


def _stamp(db_path, revision: str) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE alembic_version SET version_num = ?", (revision,))


def _check():
    return db_checks()[0]


def _synthetic_revision(revision: str, parent: str) -> str:
    """A revision file whose ``downgrade()`` drops a table we can observe."""
    return (
        f'revision: str = "{revision}"\n'
        f'down_revision: str | None = "{parent}"\n'
        "branch_labels = None\n"
        "depends_on = None\n"
        "import sqlalchemy as sa\n"
        "from alembic import op\n"
        "def upgrade() -> None:\n"
        '    op.create_table("orphan_leftover", sa.Column("id", sa.Integer, primary_key=True))\n'
        "def downgrade() -> None:\n"
        '    op.drop_table("orphan_leftover")\n'
    )


def _fixed_text(source: str):
    """A ``_revision_file_text`` that always returns *source*."""

    async def _text(ref: str, path: str) -> str:
        return source

    return _text


def _fixed_source(ref: str, path: str):
    """A ``find_revision_source`` that always reports *ref*/*path*."""

    async def _find(revision: str):
        return ref, path

    return _find


class TestCheck:
    async def test_registered_with_a_fix(self):
        check = _check()
        assert check.id == CHECK_ID
        assert check.fix is not None

    async def test_clean_database_is_ok(self, ctx):
        result = await _check().run(ctx)
        assert result.severity is Severity.OK
        assert result.data["stamped"] == result.data["heads"]

    async def test_uninitialised_database_is_info_not_error(self, tmp_path):
        bare = DoctorContext(config=AppConfig(data_dir=str(tmp_path)), db=None, handler=None)
        result = await _check().run(bare)
        assert result.severity is Severity.INFO

    async def test_orphan_is_an_error_that_names_the_revision(self, ctx, tmp_path):
        _stamp(tmp_path / "aq.db", ORPHAN)
        result = await _check().run(ctx)
        assert result.severity is Severity.ERROR
        assert result.fixable
        assert result.data["orphans"] == [ORPHAN]
        assert ORPHAN in result.detail
        assert "refuse to start" in result.detail

    async def test_an_unfindable_orphan_points_at_the_stamp_opt_in(self, ctx, tmp_path):
        _stamp(tmp_path / "aq.db", ORPHAN)
        result = await _check().run(ctx)
        # This revision exists on no ref, so the message must not promise a
        # downgrade it cannot perform.
        assert "not found on any local or remote ref" in result.detail
        assert STAMP_ENV in result.detail

    async def test_a_traceable_orphan_names_the_branch_and_file(self, ctx, tmp_path, monkeypatch):
        monkeypatch.setattr(
            db_checks_module,
            "find_revision_source",
            _fixed_source("refs/remotes/origin/aq/bold-dune-47", "migrations/versions/x.py"),
        )
        _stamp(tmp_path / "aq.db", ORPHAN)
        result = await _check().run(ctx)
        assert "defined on refs/remotes/origin/aq/bold-dune-47" in result.detail
        assert "migrations/versions/x.py" in result.detail
        assert "--fix" in result.detail


class TestProvenance:
    async def test_a_real_revision_is_traced_to_the_file_that_declares_it(self):
        """``f2a4c6e8b0d2`` is the incident's revision; find where it lives."""
        found = await find_revision_source(REAL_ORPHAN)
        if found is None:
            pytest.skip("no ref in this checkout declares the revision")
        _ref, path = found
        assert path.startswith("migrations/versions/")
        assert REAL_ORPHAN in path, "must return the file that declares it"

    async def test_a_merge_revision_that_only_mentions_it_is_not_the_source(self):
        """A merge revision lists parents in ``down_revision``, not ``revision``.

        A substring search reports that file instead of the real one, and the
        fix then tries to downgrade through a tuple parent and fails.
        """
        found = await find_revision_source(REAL_ORPHAN)
        if found is None:
            pytest.skip("no ref in this checkout declares the revision")
        assert "merge" not in found[1]

    async def test_an_invented_revision_is_not_found(self):
        assert await find_revision_source(ORPHAN) is None

    async def test_a_non_revision_string_is_never_turned_into_a_regex(self):
        assert await find_revision_source("a' or 1=1 --") is None
        assert await find_revision_source("") is None


class TestParentRevision:
    def test_plain_assignment(self):
        assert _parent_revision('down_revision = "abc123"') == "abc123"

    def test_annotated_assignment(self):
        source = 'down_revision: str | None = "abc123"\n'
        assert _parent_revision(source) == "abc123"

    def test_base_revision_has_no_parent(self):
        assert _parent_revision("down_revision = None") is None

    def test_branch_merge_tuple_is_not_a_single_parent(self):
        assert _parent_revision('down_revision = ("a", "b")') is None

    def test_unparseable_source(self):
        assert _parent_revision("def (") is None

    def test_absent_declaration(self):
        assert _parent_revision('revision = "abc"') is None


class TestFix:
    async def test_fix_refuses_to_stamp_past_an_unfindable_orphan(self, ctx, tmp_path):
        """Stamping leaves the orphan's DDL in place — it needs its own opt-in."""
        _stamp(tmp_path / "aq.db", ORPHAN)
        result = await apply_fix(_check(), ctx)
        assert result.severity is Severity.ERROR
        assert "fix failed" in result.detail
        assert STAMP_ENV in result.detail
        assert "no revision file on any ref" in result.detail
        # And it changed nothing.
        with sqlite3.connect(str(tmp_path / "aq.db")) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == ORPHAN

    @pytest.mark.migration
    async def test_a_traceable_orphan_is_undone_by_its_own_downgrade(
        self, ctx, tmp_path, monkeypatch
    ):
        """The repair that matters: run the orphan's ``downgrade()``.

        Stands in for the incident exactly — production stamped with a
        revision that lives on an unmerged branch, whose DDL is still
        applied.  ``--fix`` borrows the file from that branch, downgrades
        through it, and leaves the database at the parent this checkout
        knows, so the daemon can boot and upgrade normally.
        """
        db_path = tmp_path / "aq.db"
        head = (await _check().run(ctx)).data["heads"][0]
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE orphan_leftover (id INTEGER PRIMARY KEY)")
        _stamp(db_path, ORPHAN)

        source = _synthetic_revision(ORPHAN, head)
        monkeypatch.setattr(
            db_checks_module, "find_revision_source", _fixed_source("refs/x", "migrations/v.py")
        )
        monkeypatch.setattr(db_checks_module, "_revision_file_text", _fixed_text(source))

        result = await apply_fix(_check(), ctx)
        assert result.severity is Severity.OK, result.detail
        with sqlite3.connect(str(db_path)) as conn:
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone()[0] == head
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert "orphan_leftover" not in tables, "the orphan's downgrade() must have run"
        assert not list((db_checks_module._VERSIONS_DIR).glob("_orphan_*.py")), (
            "the borrowed revision file must not be left behind"
        )

    @pytest.mark.migration
    async def test_the_stamp_opt_in_restores_a_bootable_database(self, ctx, tmp_path, monkeypatch):
        monkeypatch.setenv(STAMP_ENV, "1")
        _stamp(tmp_path / "aq.db", ORPHAN)
        result = await apply_fix(_check(), ctx)
        assert result.severity is Severity.OK, result.detail
        assert result.fix_applied
