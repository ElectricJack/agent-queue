"""Join repair ejection (049) with the deployed later-stage checks (050).

Both revisions branch from ``a00000000048``: ``a00000000049`` reached main
with the green candidate, while ``a00000000050`` was applied to the live
database first. A database stamped at either one receives the other here,
then this point. Never re-chain ``a00000000050`` onto ``a00000000049``: a
database stamped ``a00000000050`` would then skip ``a00000000049``. See
docs/superpowers/specs/2026-10-01-join-repair-ejection-and-later-stage-migrations-design.md.

Revision ID: a00000000051
Revises: a00000000049, a00000000050
"""

revision = "a00000000051"
down_revision = ("a00000000049", "a00000000050")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Both parents are idempotent and touch disjoint objects; joining them
    # changes no schema.
    pass


def downgrade() -> None:
    pass
