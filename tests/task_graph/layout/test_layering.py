from src.task_graph.layout.layering import break_cycles, minimal_ranks
from src.task_graph.layout.model import SnapTask


def t(i, created=0.0, phase_order=None):
    return SnapTask(
        id=i,
        parent_id=None,
        is_container=False,
        status="READY",
        created_at=created,
        phase_order=phase_order,
    )


def test_chain_gets_increasing_ranks():
    kids = {i: t(i) for i in "abc"}
    ranks = minimal_ranks(kids, [("b", "a"), ("c", "b")])  # b depends on a
    assert ranks == {"a": 0, "b": 1, "c": 2}


def test_unrelated_nodes_are_rank_zero():
    kids = {i: t(i) for i in "abc"}
    assert minimal_ranks(kids, []) == {"a": 0, "b": 0, "c": 0}


def test_longest_path_wins():
    kids = {i: t(i) for i in "abcd"}
    ranks = minimal_ranks(kids, [("d", "a"), ("b", "a"), ("c", "b"), ("d", "c")])
    assert ranks["d"] == 3


def test_positive_phase_order_is_a_floor_without_a_gate_edge():
    kids = {
        "loose": t("loose"),
        "phase": t("phase", phase_order=3),
        "dependent": t("dependent"),
        "zero": t("zero", phase_order=0),
        "bool": t("bool", phase_order=True),
    }
    ranks = minimal_ranks(kids, [("dependent", "phase")])
    # The unrelated loose task and invalid orders stay at the ordinary root
    # rank.  The phase has no gate from earlier phases, but still starts at
    # its declared order; an ordinary dependency can only move work lower.
    assert ranks == {"bool": 0, "dependent": 4, "loose": 0, "phase": 3, "zero": 0}


def test_cycle_drops_edge_with_newest_dependent():
    kids = {"a": t("a", 1.0), "b": t("b", 2.0), "c": t("c", 3.0)}
    edges = [("b", "a"), ("c", "b"), ("a", "c")]  # a -> c closes the cycle
    kept = break_cycles(kids, edges)
    # Dependent of ("c","b") is c, the newest. That edge is dropped.
    assert ("c", "b") not in kept
    assert len(kept) == 2
    ranks = minimal_ranks(kids, edges)
    assert set(ranks) == {"a", "b", "c"}


def test_edges_to_unknown_ids_are_ignored():
    kids = {"a": t("a")}
    assert minimal_ranks(kids, [("a", "zzz")]) == {"a": 0}
