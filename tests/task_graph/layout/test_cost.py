from src.task_graph.layout.constants import W_CROSS, W_SPAN, W_WRAP, W_SLACK
from src.task_graph.layout.cost import container_cost, count_crossings


def pos(ordered):
    return {cid: (float(i), float(r)) for r, rank in enumerate(ordered) for i, cid in enumerate(rank)}


def test_no_crossings_when_dependents_under_blockers():
    ordered = [["a", "b"], ["c", "d"]]
    edges = [("c", "a"), ("d", "b")]
    assert count_crossings(ordered, pos(ordered), edges) == 0


def test_one_crossing_when_swapped():
    ordered = [["a", "b"], ["d", "c"]]
    edges = [("c", "a"), ("d", "b")]
    assert count_crossings(ordered, pos(ordered), edges) == 1


def test_cost_components():
    ordered = [["a", "b"], ["d", "c"]]
    edges = [("c", "a"), ("d", "b")]
    minimal = {"a": 0, "b": 0, "c": 1, "d": 1}
    p = pos(ordered)
    # crossings 1; span |1-0| + |0-1| = 2; wrap (1-1)+(1-1)=0; slack 0
    expected = W_CROSS * 1 + W_SPAN * 2 + W_WRAP * 0 + W_SLACK * 0
    assert container_cost(ordered, p, edges, minimal, [1, 1]) == expected


def test_wrap_and_slack_are_charged():
    ordered = [["a"], [], ["b"]]  # b has slack 1 if minimal is 1
    p = {"a": (0.0, 0.0), "b": (0.0, 2.0)}
    cost = container_cost(ordered, p, [], {"a": 0, "b": 1}, [2, 0, 1])
    assert cost == W_WRAP * 1 + W_SLACK * 1
