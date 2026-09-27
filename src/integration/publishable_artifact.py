"""The branch rule shared by development readiness and publication."""

from sqlalchemy import cast, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.elements import ColumnElement

#: Historical publisher observation of an absent, commit-less source. It is no
#: longer written or read: an absent ref is never an empty artifact.
EMPTY_SOURCE_KEY = "development_empty_source"

#: ``task_metadata`` key the ``development_deliveries`` retirement revision
#: (a00000000038) writes for a live branchless task a historical manifest named
#: with a source its current completion never recorded.  JSON: ``legacy_id``,
#: ``source_sha``, ``completion_id`` (the generation it was fenced to, or null)
#: and ``reason``.  It says only that the generation has an artifact of
#: unresolved provenance, so delivery is unknown -- never that it was delivered.
LEGACY_ARTIFACT_KEY = "development_legacy_artifact"


def legacy_artifact(task):
    """``EXISTS``: *task*'s current generation has a legacy artifact of unknown provenance.

    Fenced to the completion generation the retirement revision observed: a
    later completion replaces it, and that generation's own record decides.
    """
    from src.database.tables import task_completion_records, task_metadata

    completion = task_completion_records.alias()
    latest_id = (
        select(completion.c.id).where(completion.c.task_id == task.c.id)
        .order_by(completion.c.completed_at.desc(), completion.c.id.desc())
        .limit(1).correlate(task).scalar_subquery()
    )
    marker = task_metadata.alias()
    fact = cast(marker.c.value, JSONB)
    return select(literal(1)).where(
        marker.c.task_id == task.c.id,
        marker.c.key == LEGACY_ARTIFACT_KEY,
        fact["completion_id"].as_string().is_not_distinct_from(latest_id),
    ).correlate(task).exists()


def has_publishable_artifact(branch_name):
    """A branch identity can name a publishable source.

    Accept a SQLAlchemy column for readiness predicates or a loaded value for
    the publisher.
    """
    if isinstance(branch_name, ColumnElement):
        return branch_name.is_not(None)
    return branch_name is not None
