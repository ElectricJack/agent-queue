"""Compatibility exports; implementation lives in :mod:`owner_guards`."""

from src.integration.owner_guards import (
    parent_stage_at_entry as parent_stage_at_entry,
    parent_lock_key as parent_lock_key,
    active_parent_scope as active_parent_scope,
    parent_checkpoint_allowed_on as parent_checkpoint_allowed_on,
    ParentEngineOwnership as ParentEngineOwnership,
    parent_engine_guard as parent_engine_guard,
)
