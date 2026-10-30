"""Regeneration budgets shared by policy and the subprocess runner."""

import logging
import os

REGENERATION_TIMEOUT_SECONDS = 1800
REGENERATION_TIMEOUT_ENV = "AQ_REGENERATION_TIMEOUT_SECONDS"


def regeneration_timeout_seconds(configured: float) -> float:
    """Apply the daemon environment override before load scaling.

    Invalid overrides leave the repository's configured budget in place.
    Read at admission so queued runs use the current override.
    """
    value = os.environ.get(REGENERATION_TIMEOUT_ENV)
    if value is None:
        return configured
    try:
        seconds = int(value)
        if seconds > 0:
            return seconds
    except ValueError:
        pass
    logging.getLogger(__name__).warning(
        "Ignoring invalid %s=%r; expected a positive integer; using %gs",
        REGENERATION_TIMEOUT_ENV, value, configured,
    )
    return configured
