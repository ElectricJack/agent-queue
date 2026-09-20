"""Pure tests for `active_expansion` (Task 10 / A3: land on the active subgraph).

Active = every container with `agg_running > 0`, plus — when nothing is
running project-wide — root containers with `agg_active > 0`.  Ancestors of
a running container are always containers themselves with `agg_running > 0`
too (the aggregate is a subtree rollup, `driver.py::_refresh_aggregates`),
so the deterministic shallow-first, capped order is what keeps ancestor
chains intact under a cap rather than any separate ancestor walk.
"""

from __future__ import annotations

from src.task_graph.layout.model import LayoutRow
from src.task_graph.layout.view import active_expansion


def container(
    tid,
    *,
    container_id=None,
    depth=0,
    order_key="U",
    agg_running=0,
    agg_active=0,
    kind="container",
):
    return LayoutRow(
        task_id=tid,
        container_id=container_id,
        path="",
        depth=depth,
        rank=0,
        order_key=order_key,
        w=1,
        h=1,
        rel_x=0,
        rel_y=0,
        abs_x=0,
        abs_y=0,
        kind=kind,
        agg_running=agg_running,
        agg_active=agg_active,
    )


def test_running_leaf_three_levels_down_pulls_in_every_container_ancestor():
    # e -> p -> q -> (leaf, not modeled: its running-ness is already rolled
    # up into every ancestor's agg_running per driver.py's subtree rollup).
    e = container("e", container_id=None, depth=0, order_key="A", agg_running=1, agg_active=1)
    p = container("p", container_id="e", depth=1, order_key="A", agg_running=1, agg_active=1)
    q = container("q", container_id="p", depth=2, order_key="A", agg_running=1, agg_active=1)
    # An unrelated, quiet root container must not show up.
    z = container("z", container_id=None, depth=0, order_key="Z", agg_running=0, agg_active=0)
    rows = [z, q, e, p]

    assert active_expansion(rows, cap=10) == ["e", "p", "q"]


def test_nothing_running_opens_only_root_containers_with_active_work():
    # Root container with open work: included.
    e = container("e", container_id=None, depth=0, order_key="A", agg_running=0, agg_active=2)
    # Its child also has open work, but is not itself a root: excluded.
    p = container("p", container_id="e", depth=1, order_key="A", agg_running=0, agg_active=2)
    # A quiet root: excluded.
    z = container("z", container_id=None, depth=0, order_key="Z", agg_running=0, agg_active=0)

    assert active_expansion([e, p, z], cap=10) == ["e"]


def test_finished_only_containers_never_included():
    # A fully-finished container has agg_running == agg_active == 0.
    finished = container(
        "done", container_id=None, depth=0, order_key="A", agg_running=0, agg_active=0
    )
    # A stub row (finished container under variant="active") must never be
    # treated as a container candidate even if its aggregates were somehow
    # nonzero -- `kind` gates it out.
    stub = container(
        "stubbed",
        container_id=None,
        depth=0,
        order_key="B",
        agg_running=1,
        agg_active=1,
        kind="stub",
    )

    assert active_expansion([finished, stub], cap=10) == []


def test_cap_is_respected_shallowest_first():
    e = container("e", container_id=None, depth=0, order_key="A", agg_running=1, agg_active=1)
    p1 = container("p1", container_id="e", depth=1, order_key="A", agg_running=1, agg_active=1)
    p2 = container("p2", container_id="e", depth=1, order_key="B", agg_running=1, agg_active=1)
    q = container("q", container_id="p1", depth=2, order_key="A", agg_running=1, agg_active=1)

    assert active_expansion([q, p2, p1, e], cap=3) == ["e", "p1", "p2"]
    assert active_expansion([q, p2, p1, e], cap=1) == ["e"]


def test_running_takes_priority_over_active_root_fallback():
    # When anything is running anywhere, the "nothing running" root fallback
    # never engages, even for a root container with its own open work.
    running_leaf_container = container(
        "e", container_id=None, depth=0, order_key="A", agg_running=1, agg_active=1
    )
    quiet_root_with_open_work = container(
        "z", container_id=None, depth=0, order_key="Z", agg_running=0, agg_active=3
    )

    assert active_expansion(
        [running_leaf_container, quiet_root_with_open_work], cap=10
    ) == ["e"]


def test_empty_rows_returns_empty_list():
    assert active_expansion([], cap=10) == []
