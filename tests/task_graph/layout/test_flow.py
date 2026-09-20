import pytest

from src.task_graph.layout.constants import (
    CARD_H,
    CARD_W,
    HEADER_H,
    LINE_GAP,
    PADDING,
    ROW_ASPECT,
    SIBLING_GAP,
    TARGET_ROW_WIDTH,
    TARGET_ROW_WIDTH_ROOT,
    band_up,
)
from src.task_graph.layout.flow import cells_for_box, flow_container, row_target


def unit(ids):
    return {i: (CARD_W, CARD_H) for i in ids}


def test_comfortable_spacing_constants_are_dense_but_positive():
    assert (SIBLING_GAP, LINE_GAP, PADDING, TARGET_ROW_WIDTH) == (0.15, 0.22, 0.1, 4.5)
    assert ROW_ASPECT == 1.3


def test_single_rank_flows_left_to_right():
    r = flow_container([["a", "b", "c"]], unit("abc"), is_root=False)
    assert r.positions["a"] == (0.0, 0.0)
    assert r.positions["b"] == (CARD_W + SIBLING_GAP, 0.0)
    assert r.positions["c"] == (2 * (CARD_W + SIBLING_GAP), 0.0)
    assert r.lines_per_rank == [1]


def test_rank_wraps_at_target_width():
    # Comfortable target 4.5: five cards need 5*1 + 4*0.15 = 5.6, so the 5th wraps.
    r = flow_container([["a", "b", "c", "d", "e"]], unit("abcde"), is_root=False)
    assert r.positions["e"] == (0.0, CARD_H + LINE_GAP)
    assert r.lines_per_rank == [2]


def test_long_serial_chain_wraps_back_and_forth_with_short_turn():
    chain = ("a", "b", "c", "d", "e", "f")
    r = flow_container(
        [[cid] for cid in chain], unit(chain), is_root=False, serpentine_chains=(chain,),
    )
    # Four comfortable cards fill the first line; the next line begins at its
    # right edge and reads back to the left, keeping the d → e turn vertical.
    assert r.positions["a"] == (0.0, 0.0)
    assert r.positions["d"] == (3 * (CARD_W + SIBLING_GAP), 0.0)
    assert r.positions["e"] == (TARGET_ROW_WIDTH - CARD_W, CARD_H + LINE_GAP)
    assert r.positions["f"] == (TARGET_ROW_WIDTH - 2 * CARD_W - SIBLING_GAP, CARD_H + LINE_GAP)
    assert abs(r.positions["d"][0] - r.positions["e"][0]) <= SIBLING_GAP


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


# ── the aspect-balanced row target (reorganisation design §3.1) ────────────

#: children → (target, content w, content h, lines, allocated, today's allocated area)
ASPECT_TABLE = [
    (12, 5.8, 5.80, 3.99, 3, (6.0, 6.0), 36.0),
    (20, 11.8, 11.55, 2.77, 2, (12.0, 3.0), 72.0),
    (40, 11.8, 11.55, 5.21, 4, (12.0, 6.0), 144.0),
    (60, 23.8, 23.05, 3.99, 3, (24.0, 6.0), 144.0),
    (120, 23.8, 23.05, 7.65, 6, (24.0, 12.0), 288.0),
    (500, 47.8, 47.20, 16.19, 13, (48.0, 24.0), 1152.0),
]


def cards(n):
    ids = [f"c{i}" for i in range(n)]
    return [ids], unit(ids)


def test_row_target_returns_the_floor_for_a_small_scope():
    """Below the floor the ideal target is ignored, so every small scope
    keeps today's coordinates exactly."""
    for n in (4, 8):
        _, sizes = cards(n)
        assert row_target(sizes, is_root=False) == TARGET_ROW_WIDTH
        assert row_target(sizes, is_root=True) == TARGET_ROW_WIDTH_ROOT
    assert row_target({}, is_root=False) == TARGET_ROW_WIDTH
    assert row_target({}, is_root=True) == TARGET_ROW_WIDTH_ROOT


def test_row_target_balances_the_aspect_ratio():
    """§3.1's table, including the allocated box the canvas actually draws:
    the allocated area never grows over today's constant-width flow."""
    for n, target, cw, ch, lines, allocated, today_area in ASPECT_TABLE:
        ordered, sizes = cards(n)
        assert row_target(sizes, is_root=False) == pytest.approx(target), n
        r = flow_container(ordered, sizes, is_root=False, target=target)
        assert r.content[0] == pytest.approx(cw, abs=5e-3), n
        assert r.content[1] == pytest.approx(ch, abs=5e-3), n
        assert r.lines_per_rank == [lines], n
        assert r.allocated == allocated, n
        assert allocated[0] * allocated[1] <= today_area, n


def test_row_target_is_never_narrower_than_the_widest_child():
    """A 12-unit epic no longer leaves its line-mates packed into 7.0."""
    ids = ["epic"] + [f"c{i}" for i in range(8)]
    sizes = {"epic": (12.0, 6.0), **unit(ids[1:])}
    target = row_target(sizes, is_root=True)
    assert target >= 12.0
    r = flow_container([ids], sizes, is_root=True, target=target)
    assert r.positions["epic"] == (0.0, 0.0)
    assert r.positions["c7"] == (pytest.approx(20.20), 0.0)
    assert r.lines_per_rank == [1]


def test_content_lands_under_its_growth_band():
    """The band-aligned target keeps the content just under the growth band
    it is allocated at, so a wider box never snaps up a whole extra band."""
    for n, target, *_ in ASPECT_TABLE:
        ordered, sizes = cards(n)
        content_w = flow_container(ordered, sizes, is_root=False, target=target).content[0]
        assert content_w <= band_up(content_w), n
        assert band_up(content_w) - content_w < 2 * PADDING + CARD_W, n


def test_serpentine_threshold_uses_the_floor_not_the_widened_target():
    """The rank-wrap target widens; chain folding must not follow it, or an
    8-card chain becomes an 8-rank vertical stack and the turn draws a long
    diagonal instead of a short connector."""
    chain = tuple(f"s{i}" for i in range(8))
    sizes = {"wide": (11.0, 1.0), **unit(chain)}
    target = row_target(sizes, is_root=False)
    assert target == pytest.approx(11.8)
    r = flow_container(
        [["wide"], *([cid] for cid in chain)],
        sizes,
        is_root=False,
        serpentine_chains=(chain,),
        target=target,
        chain_target=TARGET_ROW_WIDTH,
    )
    assert len({r.positions[cid][1] for cid in chain}) > 1  # folded, not stacked
    assert r.positions["s4"][0] == pytest.approx(TARGET_ROW_WIDTH - CARD_W)
    assert max(r.positions[cid][0] for cid in chain) < target - CARD_W


def test_cells_for_box_covers_all_overlapped_cells():
    assert cells_for_box(0.0, 0.0, 1.0, 1.0) == [(0, 0)]
    assert cells_for_box(7.5, 0.0, 1.0, 1.0) == [(0, 0), (1, 0)]
    assert cells_for_box(0.0, 0.0, 16.0, 8.0) == [(0, 0), (1, 0)]
    assert cells_for_box(-0.5, -0.5, 1.0, 1.0) == [(-1, -1), (-1, 0), (0, -1), (0, 0)]
