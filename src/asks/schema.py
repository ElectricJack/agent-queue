"""PostgreSQL immutable audit guard shared by fresh-metadata creation.

Revision 91 freezes its own copy; later application changes cannot rewrite the
meaning of an already-deployed migration.
"""

from sqlalchemy import event, inspect

IMMUTABLE_SQL = """
CREATE OR REPLACE FUNCTION human_ask_immutable_v1() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'human ask audit records are immutable' USING ERRCODE = '23514';
END;
$$
"""


def install_ask_audit_guards(bind) -> None:
    bind.exec_driver_sql(IMMUTABLE_SQL)
    for table in ("ask_decisions", "approved_document_snapshots"):
        if not inspect(bind).has_table(table):
            continue
        bind.exec_driver_sql(f"DROP TRIGGER IF EXISTS tr_{table}_immutable ON {table}")
        bind.exec_driver_sql(
            f"CREATE TRIGGER tr_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION human_ask_immutable_v1()"
        )


def register_ask_schema_events(metadata) -> None:
    @event.listens_for(metadata, "after_create")
    def _after_create(_metadata, connection, **_kw):
        install_ask_audit_guards(connection)
