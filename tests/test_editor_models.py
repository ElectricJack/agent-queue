"""Invariant coverage for the session-owned editor model primitives."""

import pytest

from src.editor.models import Layer, Level, Page, VoxelGrid


def test_model_invariants_for_bounds_duplicate_slots_and_clone_independence():
    grid = VoxelGrid(width=1, height=1, depth=1)
    with pytest.raises(ValueError, match="out of bounds"):
        grid.set(1, 0, 0, 1)

    layer = Layer(id="layer-1")
    layer.add_page("page-1", 2, 3)
    with pytest.raises(ValueError, match="already occupied"):
        layer.add_page("page-2", 2, 3)

    level = Level(layers=[layer])
    # SES-1: a missing id is a caller error, not a successful no-op.  Failing
    # explicitly keeps misspelled/stale ids from hiding an unapplied edit.
    with pytest.raises(ValueError, match="Layer 'missing' not found"):
        level.reorder_layer("missing", 4)
    assert layer.z_depth == 0

    page = Page(name="source", content={"nested": [1]})
    page.voxel_grid.set(0, 0, 0, 2)
    clone = page.clone()
    clone.content["nested"].append(3)
    clone.voxel_grid.set(0, 0, 0, 9)
    assert page.content == {"nested": [1]}
    assert page.voxel_grid.get(0, 0, 0) == 2
