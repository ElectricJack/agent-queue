"""Regression: the work-graph data migration survives a rollback round-trip.

``dep_type`` is part of ``task_dependencies``' primary key, so the plan-edge
retype in ``a1c7f3e08b42`` cannot be a bare ``UPDATE … SET dep_type``: once
typed edges exist the same pair can legally carry both ``blocks`` and
``parent-child``, and retyping one onto the other violates the PK.

The reachable failure is the **downgrade** — a legacy DB is all-``blocks`` on
first upgrade, but a post-upgrade DB has the supervisor's ``parent-child``
edge sitting next to a hand-added ``blocks`` edge for the same pair.  These
tests seed exactly that pair and drive ``up → down → up``.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine


WORK_GRAPH_REVISION = "a1c7f3e08b42"
PRIOR_REVISION = "93a8a9e48fb8"


def _alembic_config(db_url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _seed_plan_pair(conn, *, edge_types: tuple[str, ...]) -> None:
    """A plan parent ``plan`` with subtask ``s1`` carrying *edge_types*."""
    now = time.time()
    conn.execute(
        sa.text(
            "INSERT INTO projects (id, name, created_at) "
            "VALUES ('p-wg', 'wg', :now)"
        ),
        {"now": now},
    )
    for tid, parent, is_sub, status in (
        ("plan", None, 0, "IN_PROGRESS"),
        ("s1", "plan", 1, "DEFINED"),
    ):
        conn.execute(
            sa.text(
                "INSERT INTO tasks (id, project_id, parent_task_id, title, description,"
                " priority, status, verification_type, retry_count, max_retries,"
                " is_plan_subtask, attachments, created_at, updated_at)"
                " VALUES (:id, 'p-wg', :parent, :id, '', 3, :status, 'automated', 0, 3,"
                " :is_sub, '[]', :now, :now)"
            ),
            {"id": tid, "parent": parent, "is_sub": is_sub, "status": status, "now": now},
        )
    for dep_type in edge_types:
        conn.execute(
            sa.text(
                "INSERT INTO task_dependencies (task_id, depends_on_task_id, dep_type)"
                " VALUES ('s1', 'plan', :dep_type)"
            ),
            {"dep_type": dep_type},
        )


def _edge_types(engine) -> list[str]:
    with engine.connect() as conn:
        return sorted(
            r[0]
            for r in conn.execute(
                sa.text(
                    "SELECT dep_type FROM task_dependencies "
                    "WHERE task_id = 's1' AND depends_on_task_id = 'plan'"
                )
            ).fetchall()
        )


def test_round_trip_with_a_colliding_pair():
    """``(s1, plan)`` carrying *both* edge types survives up → down → up.

    Before the fix this raised
    ``sqlite3.IntegrityError: UNIQUE constraint failed:
    task_dependencies.task_id, task_dependencies.depends_on_task_id,
    task_dependencies.dep_type`` on the downgrade.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "wg.db"
        cfg = _alembic_config(f"sqlite+aiosqlite:///{db_path}")
        engine = create_engine(f"sqlite:///{db_path}")

        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            _seed_plan_pair(conn, edge_types=("blocks", "parent-child"))

        # Re-running the upgrade merges the colliding pair rather than
        # duplicating it (the DELETE arm).
        command.downgrade(cfg, PRIOR_REVISION)
        assert _edge_types(engine) == ["blocks"]

        command.upgrade(cfg, WORK_GRAPH_REVISION)
        assert _edge_types(engine) == ["parent-child"]

        command.downgrade(cfg, PRIOR_REVISION)
        assert _edge_types(engine) == ["blocks"]

        command.upgrade(cfg, WORK_GRAPH_REVISION)
        assert _edge_types(engine) == ["parent-child"]
        engine.dispose()


def test_legacy_blocks_only_pair_retypes_and_rolls_back():
    """The non-colliding path is unchanged: one edge, retyped both ways."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "wg.db"
        cfg = _alembic_config(f"sqlite+aiosqlite:///{db_path}")
        engine = create_engine(f"sqlite:///{db_path}")

        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            _seed_plan_pair(conn, edge_types=("blocks",))

        command.downgrade(cfg, PRIOR_REVISION)
        assert _edge_types(engine) == ["blocks"]
        command.upgrade(cfg, WORK_GRAPH_REVISION)
        assert _edge_types(engine) == ["parent-child"]
        command.downgrade(cfg, PRIOR_REVISION)
        assert _edge_types(engine) == ["blocks"]
        engine.dispose()


def test_non_plan_edges_are_left_alone():
    """An edge that is not a plan-subtask→parent edge keeps its type."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "wg.db"
        cfg = _alembic_config(f"sqlite+aiosqlite:///{db_path}")
        engine = create_engine(f"sqlite:///{db_path}")

        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            _seed_plan_pair(conn, edge_types=("blocks",))
            # `s1` is a plan subtask of `plan`, but this edge points elsewhere.
            conn.execute(
                sa.text(
                    "INSERT INTO tasks (id, project_id, title, description, priority,"
                    " status, verification_type, retry_count, max_retries,"
                    " is_plan_subtask, attachments,"
                    " created_at, updated_at)"
                    " VALUES ('other', 'p-wg', 'other', '', 3, 'DEFINED', 'automated',"
                    " 0, 3, 0, '[]', :now, :now)"
                ),
                {"now": time.time()},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO task_dependencies (task_id, depends_on_task_id, dep_type)"
                    " VALUES ('s1', 'other', 'blocks')"
                )
            )

        command.downgrade(cfg, PRIOR_REVISION)
        command.upgrade(cfg, WORK_GRAPH_REVISION)
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT dep_type FROM task_dependencies "
                    "WHERE task_id = 's1' AND depends_on_task_id = 'other'"
                )
            ).fetchone()
        assert row[0] == "blocks"
        engine.dispose()
