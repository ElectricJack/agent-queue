"""Ready playbook private-value services for engine tests.

The engine refuses a ``save_result_as`` on a wait/agent_task step, and a
command step with private inputs or results, unless
``EngineServices.private_values`` is set and ``check_ready()`` passes. Tests
that drive real playbooks wire one of these in.
"""

from __future__ import annotations

from typing import Any

TEST_KEY_ID = "test"
TEST_KEY = b"x" * 32


def private_value_service(db: Any) -> Any:
    """A real, ready ``PrivateValueService`` on ``db``'s engine, installed on ``db``.

    Use it whenever the test has a real PostgreSQL ``Database``: run snapshots
    persisted through that database then encrypt and resolve private bindings
    with the same key the engine checks.
    """
    from src.playbooks.private_values import PrivateValueService

    service = PrivateValueService(
        db._engine, active_key_id=TEST_KEY_ID, keys={TEST_KEY_ID: TEST_KEY}
    )
    db.set_playbook_private_values(service)
    return service


class ReadyPrivateValues:
    """A ready stub for engines over an in-memory run repository.

    ``RecordingRunRepository`` keeps snapshots in memory and never encrypts,
    so the engine only ever asks this service whether storage is ready.
    """

    def require_ready(self) -> tuple[str, bytes]:
        return TEST_KEY_ID, TEST_KEY

    async def check_ready(self) -> None:
        return None
