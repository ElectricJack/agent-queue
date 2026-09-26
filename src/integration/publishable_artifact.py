"""The branch rule shared by development readiness and publication."""

from sqlalchemy import cast, literal, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.elements import ColumnElement

EMPTY_SOURCE_KEY = "development_empty_source"


def development_empty_source(task, repository_id):
    """A fetched absent source with no commits, for this exact task revision.

    Preserve canonical branch identities. Reopening, changing the task, or
    recording another completion invalidates the publisher's observation.
    """
    from src.database.tables import task_completion_records, task_metadata

    completion = task_completion_records.alias()
    latest_id = (
        select(completion.c.id).where(completion.c.task_id == task.c.id)
        .order_by(completion.c.completed_at.desc(), completion.c.id.desc())
        .limit(1).correlate(task).scalar_subquery()
    )
    observation = task_metadata.alias()
    fact = cast(observation.c.value, JSONB)
    correlated = (
        (task, repository_id.table) if isinstance(repository_id, ColumnElement) else (task,)
    )
    return select(literal(1)).where(
        observation.c.task_id == task.c.id,
        observation.c.key == EMPTY_SOURCE_KEY,
        fact["repository_id"].as_string() == repository_id,
        fact["branch_name"].as_string() == task.c.branch_name,
        fact["task_updated_at"].as_float() == task.c.updated_at,
        fact["completion_id"].as_string().is_not_distinct_from(latest_id),
    ).correlate(*correlated).exists()


def has_publishable_artifact(branch_name):
    """A branch identity can name a publishable source.

    Accept a SQLAlchemy column for readiness predicates or a loaded value for
    the publisher. Empty-source observations additionally exempt completed
    revisions whose branch has no artifact, without renaming that identity.
    """
    if isinstance(branch_name, ColumnElement):
        return branch_name.is_not(None)
    return branch_name is not None
