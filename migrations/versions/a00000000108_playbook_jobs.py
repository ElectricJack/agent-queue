"""Real playbook job owners and retention-scoped immutable input pins."""
import sqlalchemy as sa
from alembic import op

revision = "a00000000108"
down_revision = "a00000000107"
branch_labels = None
depends_on = None


def upgrade():
    from src.database.tables import jobs, job_input_pins

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("jobs")}
    for name in ("playbook_run_id", "playbook_step_id", "iteration_path", "attempt",
                 "artifact_digest", "input_root"):
        if name not in columns:
            op.add_column("jobs", sa.Column(name, sa.Integer if name == "attempt" else sa.Text))
    if not any(f["constrained_columns"] == ["playbook_run_id"]
               for f in sa.inspect(bind).get_foreign_keys("jobs")):
        op.create_foreign_key("fk_jobs_playbook_run", "jobs", "playbook_v2_runs",
                              ["playbook_run_id"], ["run_id"], ondelete="RESTRICT")
    for name in ("project_id", "workspace_id", "workspace_generation"):
        op.alter_column("jobs", name, nullable=True)
    checks = {c["name"] for c in sa.inspect(bind).get_check_constraints("jobs")}
    if "ck_jobs_owner_kind" in checks:
        op.drop_constraint("ck_jobs_owner_kind", "jobs", type_="check")
    for name in ("ck_jobs_owner_kind", "ck_jobs_owner_fields"):
        if name == "ck_jobs_owner_kind" or name not in checks:
            constraint = next(c for c in jobs.constraints if c.name == name)
            op.create_check_constraint(name, "jobs", str(constraint.sqltext))
    next(i for i in jobs.indexes if i.name == "uq_jobs_playbook_operation").create(
        bind, checkfirst=True)
    job_input_pins.create(bind, checkfirst=True)


def downgrade():
    # Roll back code only after settling scripts; retain their ownership/evidence.
    pass
