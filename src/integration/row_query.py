"""Single-row reads in the caller's integration transaction."""

from sqlalchemy import select


async def select_row(conn, columns, *conditions, lock=False):
    """Keep SQLAlchemy's zero/one/many cardinality semantics and optional row lock."""
    statement = select(columns).where(*conditions)
    return (
        (await conn.execute(statement.with_for_update() if lock else statement))
        .mappings()
        .one_or_none()
    )
