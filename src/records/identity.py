"""Installation-stable identities; titles and hosts never participate."""

from uuid import UUID, uuid4, uuid5


class RecordIntegrityError(ValueError):
    """A permanent identity is inconsistent with its authoritative domain."""


def task_record_id(installation_id: UUID, task_id: str) -> UUID:
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("task_id must be a nonempty string")
    return uuid5(installation_id, f"task:{task_id}")


def knowledge_identity() -> tuple[UUID, str]:
    record_id = uuid4()
    return record_id, f"kn-{record_id.hex}"


def revision_id() -> UUID:
    return uuid4()
