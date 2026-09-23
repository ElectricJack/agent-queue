"""The branch rule shared by development readiness and publication."""

from sqlalchemy.sql.elements import ColumnElement


def has_publishable_artifact(branch_name):
    """A task has a source to publish exactly when it owns a branch.

    Accept a SQLAlchemy column for readiness predicates or a loaded value for
    the publisher. Both paths must make the same branchless decision.
    """
    if isinstance(branch_name, ColumnElement):
        return branch_name.is_not(None)
    return branch_name is not None
