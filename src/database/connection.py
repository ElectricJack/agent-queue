"""Database connection management and initialization.

This module is kept for backward compatibility.  The actual connection
lifecycle, schema creation, and migrations are now handled by
:mod:`src.database.engine`.

Legacy imports that reference ``create_sqlite_connection`` or
``run_startup_migrations`` will find stubs here that delegate to the
new engine-based functions.
"""

from __future__ import annotations

# Re-export for any code that still imports from here
from src.database.engine import (  # noqa: F401
    create_sqlite_engine,
    run_schema_setup,
    run_startup_data_migrations,
)
