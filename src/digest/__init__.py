"""Hourly digest: deterministic eligibility, aggregation and rendering.

Discord's routine surface is one short message an hour (implementation spec
§8).  Everything that decides *whether* to send and *what the text says*
lives here, and it is deliberately transport-free: no Discord objects, no
network, no LLM call, and no wall clock of its own.  The caller supplies the
window, the durable facts and the live-attempt snapshot; the same
:func:`~src.digest.aggregate.build_digest` serves the operator's dry preview
and the real delivery, so a preview cannot disagree with what would be sent.

The database read that gathers those inputs is
:mod:`src.database.queries.digest_queries`; it is the only part that touches
rows, and it is kept separate so eligibility stays testable as a pure table.
"""

from src.digest.aggregate import DigestResult, build_digest
from src.digest.eligibility import Eligibility, evaluate_eligibility
from src.digest.facts import (
    CATEGORIES,
    ActiveTask,
    DigestInputs,
    DigestWindow,
    WorkFact,
)
from src.digest.render import render_digest
from src.digest.schedule import (
    DigestSchedule,
    config_generation,
    destination_id,
    schedule_for,
    validate_settings,
)

__all__ = [
    "CATEGORIES",
    "ActiveTask",
    "DigestInputs",
    "DigestResult",
    "DigestSchedule",
    "DigestWindow",
    "Eligibility",
    "WorkFact",
    "build_digest",
    "config_generation",
    "destination_id",
    "evaluate_eligibility",
    "render_digest",
    "schedule_for",
    "validate_settings",
]
