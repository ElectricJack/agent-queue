from src.task_graph.layout.constants import (
    CARD_H, CARD_W, HEADER_H, LINE_GAP, PADDING, SIBLING_GAP, band_up,
)
from src.task_graph.layout.flow import cells_for_box, flow_container


def unit(ids):
    return {i: (CARD_W, CARD_H) for i in ids}


def test_single_rank_flows_left_to_right():
    r = flow_container([["a", "b", "c"]], unit("abc"), is_root=False)
    assert r.positions["a"] == (0.0, 0.0)
    assert r.positions["b"] == (CARD_W + SIBLING_GAP, 0.0)
    assert r.positions["c"] == (2 * (CARD_W + SIBLING_GAP), 0.0)
    assert r.lines_per_rank == [1]


def test_rank_wraps_at_target_width():
    # Target 4.0 units: four cards need 4*1 + 3*0.2 = 4.6 > 4.0, so the 4th wraps.
    r = flow_container([["a", "b", "c", "d"]], unit("abcd"), is_root=False)
    assert r.positions["d"] == (0.0, CARD_H + LINE_GAP)
    assert r.lines_per_rank == [2]


def test_second_rank_starts_below_first():
    r = flow_container([["a"], ["b"]], unit("ab"), is_root=False)
    assert r.positions["b"] == (0.0, CARD_H + LINE_GAP)


def test_line_height_is_tallest_child():
    sizes = {"a": (1.0, 1.0), "b": (1.0, 3.0), "c": (1.0, 1.0)}
    r = flow_container([["a", "b"], ["c"]], sizes, is_root=False)
    assert r.positions["c"][1] == 3.0 + LINE_GAP


def test_content_and_allocated_sizes():
    r = flow_container([["a", "b"]], unit("ab"), is_root=False)
    w = 2 * CARD_W + SIBLING_GAP + 2 * PADDING
    h = CARD_H + 2 * PADDING + HEADER_H
    assert r.content == (w, h)
    assert r.allocated == (band_up(w), band_up(h))


def test_empty_container_is_card_sized():
    r = flow_container([], {}, is_root=False)
    assert r.content == (CARD_W, CARD_H)
    assert r.allocated == (CARD_W, CARD_H)


def test_cells_for_box_covers_all_overlapped_cells():
    assert cells_for_box(0.0, 0.0, 1.0, 1.0) == [(0, 0)]
    assert cells_for_box(7.5, 0.0, 1.0, 1.0) == [(0, 0), (1, 0)]
    assert cells_for_box(0.0, 0.0, 16.0, 8.0) == [(0, 0), (1, 0)]
    assert cells_for_box(-0.5, -0.5, 1.0, 1.0) == [(-1, -1), (-1, 0), (0, -1), (0, 0)]
