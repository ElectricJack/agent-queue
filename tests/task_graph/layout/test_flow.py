import random
from unittest import mock

import pytest

from src.task_graph.layout import flow as flow_module
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
    growth_bands,
)
from src.task_graph.layout.flow import (
    cells_for_box,
    clamp_row_target,
    flow_container,
    row_target,
    row_target_rungs,
)


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

#: children → (ideal target, content w, content h, lines, raw allocated, published target)
#: The published target includes the final-order clamp.  In particular, the
#: 60-card candidate's more balanced raw box would cost more area than that
#: same ordering's floor baseline, so it is intentionally stepped down.
ASPECT_TABLE = [
    (12, 5.8, 5.80, 3.99, 3, (6.0, 6.0), 5.8),
    (20, 11.8, 11.55, 2.77, 2, (12.0, 3.0), 11.8),
    (40, 11.8, 11.55, 5.21, 4, (12.0, 6.0), 11.8),
    (60, 16.6, 16.15, 6.43, 5, (16.8, 12.0), 5.8),
    (120, 23.32, 23.05, 7.65, 6, (23.52, 12.0), 16.6),
    (500, 45.91, 46.05, 16.19, 13, (46.11, 16.8), 45.91),
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
    """The approved finer ladder's committed fixture shapes.

    This is the ideal geometry before the final-order clamp.  The clamp is
    tested separately because the design deliberately does not promise that
    every old/new candidate has equal area.
    """
    for n, target, cw, ch, lines, allocated, _ in ASPECT_TABLE:
        ordered, sizes = cards(n)
        assert row_target(sizes, is_root=False) == pytest.approx(target), n
        r = flow_container(ordered, sizes, is_root=False, target=target)
        assert r.content[0] == pytest.approx(cw, abs=5e-3), n
        assert r.content[1] == pytest.approx(ch, abs=5e-3), n
        assert r.lines_per_rank == [lines], n
        assert r.allocated == pytest.approx(allocated), n


def test_finer_ladder_uses_exact_hundredth_rungs_above_twelve():
    assert growth_bands(up_to=64.56) == (1.5, 3.0, 6.0, 12.0, 16.8, 23.52, 32.93, 46.11, 64.56)
    assert band_up(16.8) == 16.8
    assert band_up(16.800001) == 23.52


def test_finer_ladder_makes_the_large_epic_candidate_more_balanced():
    """The old 60-card candidate was 24 × 6 (4:1); the approved rung is
    16.8 × 12 (1.4:1) before the no-growth publication clamp applies."""
    ordered, sizes = cards(60)
    target = row_target(sizes, is_root=False)
    candidate = flow_container(ordered, sizes, is_root=False, target=target)
    assert target == pytest.approx(16.6)
    assert candidate.allocated == pytest.approx((16.8, 12.0))
    assert candidate.allocated[0] / candidate.allocated[1] < 4.0


def test_row_target_is_never_narrower_than_the_widest_child():
    """A 12-unit epic no longer leaves its line-mates packed into 7.0."""
    ids = ["epic"] + [f"c{i}" for i in range(8)]
    sizes = {"epic": (12.0, 6.0), **unit(ids[1:])}
    target = row_target(sizes, is_root=True)
    assert target == pytest.approx(16.6)
    r = flow_container([ids], sizes, is_root=True, target=target)
    assert r.positions["epic"] == (0.0, 0.0)
    assert r.positions["c3"] == (pytest.approx(15.60), 0.0)
    assert r.positions["c7"] == (pytest.approx(3.45), pytest.approx(6.22))
    assert r.lines_per_rank == [2]


def test_content_lands_under_its_growth_band():
    """The band-aligned target keeps the content just under the growth band
    it is allocated at, so a wider box never snaps up a whole extra band:
    the content's own band is exactly the band the target was derived from."""
    for n, target, *_ in ASPECT_TABLE:
        ordered, sizes = cards(n)
        content_w = flow_container(ordered, sizes, is_root=False, target=target).content[0]
        assert band_up(content_w) == pytest.approx(target + 2 * PADDING), n
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


# ── the never-grows clamp (t24 finding F1) ────────────────────────────────

#: Mixed scopes whose ideal target widens the drawn box instead of shrinking
#: it. Both double their allocated area without the clamp.
CLAMP_COUNTEREXAMPLES = [
    {"pkg": (3.0, 6.0), **{f"c{i}": (CARD_W, CARD_H) for i in range(4)}},
    {"a": (3.0, 3.0), "b": (1.0, 1.0), "c": (1.0, 3.0), "d": (1.0, 6.0)},
]


def allocated_area(result):
    return result.allocated[0] * result.allocated[1]


def flow_at(ordered, sizes, target, *, is_root=False):
    return flow_container(ordered, sizes, is_root=is_root, target=target)


def test_a_widened_target_that_would_grow_the_drawn_box_is_stepped_down():
    """The aspect-balanced ideal is not always an improvement: for a scope
    holding one tall child plus small ones it buys width the growth ladder
    then charges a whole band for. The clamp walks back down the ladder."""
    for sizes in CLAMP_COUNTEREXAMPLES:
        ordered = [sorted(sizes)]
        ideal = row_target(sizes, is_root=False)
        assert ideal > TARGET_ROW_WIDTH  # the ideal really did widen
        assert allocated_area(flow_at(ordered, sizes, ideal)) > allocated_area(
            flow_at(ordered, sizes, TARGET_ROW_WIDTH)
        )  # ... and unclamped it would have cost a whole extra band
        clamped = clamp_row_target(ordered, sizes, is_root=False, target=ideal)
        assert clamped < ideal
        assert allocated_area(flow_at(ordered, sizes, clamped)) <= allocated_area(
            flow_at(ordered, sizes, TARGET_ROW_WIDTH)
        )


def test_the_clamp_does_not_fire_on_the_unit_card_table():
    """The clamp preserves the published target captured in each fixture."""
    for n, target, *_, published_target in ASPECT_TABLE:
        ordered, sizes = cards(n)
        assert clamp_row_target(ordered, sizes, is_root=False, target=target) == published_target, n


def _mixed_scopes(seed, count):
    """Seeded mixed-size scopes with random rank partitions and orderings."""
    rnd = random.Random(seed)
    widths = (1.0, 1.0, 1.0, 3.0, 6.0, 12.0, 24.0)
    heights = (1.0, 1.0, 3.0, 6.0, 12.0)
    for _ in range(count):
        n = rnd.randint(1, 30)
        sizes = {
            f"c{i}": (rnd.choice(widths), rnd.choice(heights)) for i in range(n)
        }
        ids = list(sizes)
        rnd.shuffle(ids)
        buckets: list[list[str]] = [[] for _ in range(rnd.randint(1, max(1, n // 3)))]
        for cid in ids:
            buckets[rnd.randrange(len(buckets))].append(cid)
        yield sizes, [b for b in buckets if b]


def test_the_allocated_area_never_grows_for_any_mixed_scope():
    """F1's headline property, enforced by construction rather than hoped
    for: over 400 seeded mixed-size scopes, at every rank partition and
    ordering, the clamped target's drawn box is never bigger than the
    floor's."""
    checked = 0
    stepped_down = 0
    for sizes, ordered in _mixed_scopes(seed=20260920, count=400):
        ideal = row_target(sizes, is_root=False)
        target = clamp_row_target(ordered, sizes, is_root=False, target=ideal)
        assert TARGET_ROW_WIDTH <= target <= ideal
        base = allocated_area(flow_at(ordered, sizes, TARGET_ROW_WIDTH))
        got = allocated_area(flow_at(ordered, sizes, target))
        assert got <= base + 1e-9, (sizes, ordered, target)
        checked += 1
        stepped_down += target < ideal
    assert checked == 400
    # The property would be vacuous if the clamp never had to do anything.
    assert stepped_down > 0


def test_the_root_keeps_the_unclamped_ideal():
    """The root is never banded, so F1's "a whole extra band" failure mode
    does not exist for it, and it has no parent to push. Area is also the
    wrong measure there: symptom 1's fix trades width for height on purpose,
    and the operator's own screenshot gains area by doing so. Clamping the
    root would throw that fix away, so the engine does not clamp it —
    pinned here on the screenshot's own numbers."""
    ids = ["epic"] + [f"c{i}" for i in range(8)]
    sizes = {"epic": (12.0, 6.0), **unit(ids[1:])}
    ideal = row_target(sizes, is_root=True)
    floor_flow = flow_at([ids], sizes, TARGET_ROW_WIDTH_ROOT, is_root=True)
    ideal_flow = flow_at([ids], sizes, ideal, is_root=True)
    assert floor_flow.lines_per_rank == [3] and ideal_flow.lines_per_rank == [2]
    assert ideal_flow.content[1] < floor_flow.content[1]  # shorter …
    assert allocated_area(ideal_flow) > allocated_area(floor_flow)  # … but larger
    # So the clamp, if it were applied here, WOULD step this back down.
    assert clamp_row_target([ids], sizes, is_root=True, target=ideal) < ideal


def test_the_clamp_is_bounded_by_the_ladder():
    """Its cost is O(ladder) flow passes, once per container pass — and a
    scope whose target is already the floor costs nothing at all."""
    calls = {"n": 0}
    real = flow_module.flow_container

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    ordered, sizes = cards(500)
    target = row_target(sizes, is_root=False)
    bound = 1 + len(row_target_rungs(TARGET_ROW_WIDTH, up_to=target))
    with mock.patch.object(flow_module, "flow_container", counting):
        flow_module.clamp_row_target(ordered, sizes, is_root=False, target=target)
        assert 1 <= calls["n"] <= bound
        calls["n"] = 0
        flow_module.clamp_row_target(
            [["a"]], unit("a"), is_root=False, target=TARGET_ROW_WIDTH
        )
        assert calls["n"] == 0
