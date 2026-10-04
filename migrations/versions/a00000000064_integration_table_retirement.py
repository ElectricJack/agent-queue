"""Archive and receipt tables for retiring legacy integration tables.

Revision ID: a00000000064
Revises: a00000000061

Additive only (rev-agile-ridge revision 2, §5.3 and §6 phase 4).  Nothing is
dropped here, and no revision drops a legacy integration table: retiring one is
the explicit operator step ``aq db retire``
(:mod:`src.integration.table_retirement`), which checks the family's
readiness and a fresh backup, copies every row into
``integration_retired_rows`` and records an ``integration_table_retirements``
receipt before it drops anything.  Triggers keep both append-only so the
archive stays the audit trail.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a00000000064"
down_revision = "a00000000061"
branch_labels = None
depends_on = None

RETIREMENTS = "integration_table_retirements"
ROWS = "integration_retired_rows"

FUNCTION = """
    CREATE OR REPLACE FUNCTION integration_table_retirement_is_append_only()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'integration table retirement archive is append-only';
    END;
    $$ LANGUAGE plpgsql
"""

TRIGGERS = tuple(
    (
        f"{table}_append_only",
        table,
        (
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION integration_table_retirement_is_append_only()"
        ),
    )
    for table in (RETIREMENTS, ROWS)
)


def _create_retirements() -> None:
    op.create_table(
        RETIREMENTS,
        sa.Column("table_name", sa.Text(), primary_key=True),
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("rows_digest", sa.Text(), nullable=False),
        sa.Column("columns", postgresql.JSONB(), nullable=False),
        sa.Column("backup_path", sa.Text(), nullable=False),
        sa.Column("backup_sha256", sa.Text(), nullable=False),
        sa.Column("schema_revision", sa.Text(), nullable=False),
        sa.Column("retired_by", sa.Text(), nullable=False),
        sa.Column("retired_at", sa.Float(), nullable=False),
        sa.CheckConstraint("row_count >= 0", name="ck_integration_table_retirements_count"),
        sa.CheckConstraint(
            "rows_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="ck_integration_table_retirements_digest",
        ),
        sa.CheckConstraint(
            "backup_sha256 ~ '^[0-9a-f]{64}$'", name="ck_integration_table_retirements_backup"
        ),
    )


def _create_rows() -> None:
    op.create_table(
        ROWS,
        sa.Column(
            "table_name",
            sa.Text(),
            sa.ForeignKey(f"{RETIREMENTS}.table_name", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.BigInteger(), primary_key=True),
        sa.Column("row_data", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("ordinal >= 1", name="ck_integration_retired_rows_ordinal"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    # The squashed baseline is built from live metadata, so a fresh install
    # already has both tables; only the triggers are installed.
    if not sa.inspect(bind).has_table(RETIREMENTS):
        _create_retirements()
    if not sa.inspect(bind).has_table(ROWS):
        _create_rows()
    op.execute(FUNCTION)
    for name, table, statement in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
        op.execute(statement)


def downgrade() -> None:
    # A receipt is history: refuse to discard an archive that holds one.
    bind = op.get_bind()
    if sa.inspect(bind).has_table(RETIREMENTS) and bind.scalar(
        sa.text(f"SELECT count(*) FROM {RETIREMENTS}")
    ):
        raise RuntimeError(
            "integration_table_retirements holds retirement receipts; restore or keep "
            "the archive instead of downgrading past a00000000064"
        )
    for table in (ROWS, RETIREMENTS):
        if sa.inspect(bind).has_table(table):
            op.drop_table(table)
    op.execute("DROP FUNCTION IF EXISTS integration_table_retirement_is_append_only()")
