"""Backend-agnostic identity for "what a fully migrated schema looks like".

Both schema template caches are keyed off this module:

* the SQLite template cache in :mod:`src.database.engine`, which byte-copies a
  fully migrated ``.db`` file for each fresh test database, and
* the PostgreSQL template database used by the test suite
  (``tests/db_fixtures.py``), which clones a fully migrated database with
  ``CREATE DATABASE ... TEMPLATE``.

The logic lives here rather than in ``engine.py`` so the test substrate can
compute a cache key without importing a production engine module — and so it
survives the SQLite removal, which deletes everything else in that cache.

See ``docs/superpowers/specs/2026-09-07-sqlite-removal-implementation.md`` §T4.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

#: Repository root — three levels up from ``src/database/schema_key.py``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


@lru_cache(maxsize=1)
def alembic_head_revisions() -> tuple[str, ...]:
    """Alembic's current heads, sorted, for cache validation and cache keys.

    Cached: building a ``ScriptDirectory`` parses every file under
    ``migrations/versions`` (114 of them today).  ``run_schema_setup``'s
    already-at-head fast path calls this on every database construction, so
    an uncached lookup would reintroduce exactly the cost it exists to avoid.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return tuple(sorted(ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_heads()))


def schema_inputs() -> list[Path]:
    """Every file whose content decides what a fully migrated schema looks like.

    ``migrations/env.py`` configures how revisions run (batch mode, the
    per-migration transaction) and revision ``b2c3d4e5f6a7`` imports
    ``src.database.hierarchy_migration``, so a change to either has to
    invalidate the template exactly as a changed revision file does.
    """
    database = PROJECT_ROOT / "src" / "database"
    migrations = PROJECT_ROOT / "migrations"
    return [
        database / "tables.py",
        database / "hierarchy_migration.py",
        migrations / "env.py",
        migrations / "integration_guards.py",
        *sorted((migrations / "versions").glob("*.py")),
    ]


@lru_cache(maxsize=1)
def schema_key() -> tuple[str, tuple[str, ...]]:
    """Hash schema inputs so a changed migration never reuses an old template.

    Returns ``(key, heads)``.  The key mixes the Alembic heads with a digest
    over every input file's *path and content*, so a renamed revision
    invalidates the template even when its body is unchanged.
    """
    digest = hashlib.sha256()
    for source in schema_inputs():
        digest.update(str(source.relative_to(PROJECT_ROOT)).encode())
        digest.update(b"\0")
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    heads = alembic_head_revisions()
    return f"{'-'.join(heads)}-{digest.hexdigest()}", heads


def schema_key_slug(length: int = 16) -> str:
    """A short lowercase-alnum form of :func:`schema_key`, safe in an identifier.

    PostgreSQL database names are capped at 63 bytes and the per-worker test
    databases already spend some of that budget, so the full key (two heads
    plus a 64-char digest) does not fit.  The digest half is uniformly
    distributed, so a 16-character prefix is ample for distinguishing the
    handful of schema versions alive on one machine at once.
    """
    key, _ = schema_key()
    return hashlib.sha256(key.encode()).hexdigest()[:length]
