"""Scoped trigger fault injection for PostgreSQL tests using reusable databases."""

from contextlib import asynccontextmanager


@asynccontextmanager
async def suspended_trigger(conn, *, table: str, name: str):
    """Temporarily permit deliberate corruption, then restore the live guard."""
    await conn.exec_driver_sql(f'ALTER TABLE "{table}" DISABLE TRIGGER "{name}"')
    try:
        yield
    finally:
        await conn.exec_driver_sql(f'ALTER TABLE "{table}" ENABLE TRIGGER "{name}"')


@asynccontextmanager
async def injected_trigger(db, *, name: str, table: str, event: str, condition: str, body: str):
    """Install trusted test SQL and remove both trigger and function on every exit."""
    function = f"{name}_function"
    async with db.immediate() as conn:
        await conn.exec_driver_sql(
            f'CREATE FUNCTION "{function}"() RETURNS trigger LANGUAGE plpgsql AS $$ '
            f"BEGIN {body} END; $$"
        )
        await conn.exec_driver_sql(
            f'CREATE TRIGGER "{name}" BEFORE {event} ON "{table}" '
            f'FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION "{function}"()'
        )
    try:
        yield
    finally:
        async with db.immediate() as conn:
            await conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}" ON "{table}"')
            await conn.exec_driver_sql(f'DROP FUNCTION IF EXISTS "{function}"()')
