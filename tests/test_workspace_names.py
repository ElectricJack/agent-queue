"""Collision fallback for human-readable workspace IDs."""

from unittest.mock import AsyncMock

import src.workspace_names as workspace_names


async def test_workspace_id_uses_numeric_suffix_after_retry_budget(monkeypatch):
    monkeypatch.setattr(workspace_names.random, "choice", lambda values: values[0])
    monkeypatch.setattr(workspace_names.random, "randint", lambda _low, _high: 42)
    db = AsyncMock()
    db.get_workspace.side_effect = [object()] * workspace_names._MAX_RETRIES + [None]
    assert await workspace_names.generate_workspace_id(db) == "ws-iron-tower-42"
    assert db.get_workspace.call_args_list[-1].args == ("ws-iron-tower-42",)
