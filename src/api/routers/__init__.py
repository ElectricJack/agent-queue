"""Auto-generated API routers for each tool_registry category.

Call ``register_all_routers(app)`` to mount all category routers
onto a FastAPI app instance.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def register_all_routers(app: FastAPI) -> int:
    """Build and register all auto-generated category routers.

    Returns the number of routes registered.
    """
    from src.api.codegen import build_category_routers

    routers = build_category_routers()
    total = 0
    for router in routers:
        app.include_router(router)
        total += len(router.routes)

    logger.info("Registered %d auto-generated API routes across %d categories", total, len(routers))
    return total
