"""Tests for the level editor brush system.

Focuses on the critical behavior: when fill_all_depths is disabled,
2D brushes (square, circle) should raycast to find the correct z-depth
and place voxels in front of existing surfaces, NOT always on the back layer.
"""


from src.editor.brush import (
    BrushConfig,
    BrushOperation,
    BrushType,
    apply_brush,
    get_brush_footprint,
    _raycast_for_placement_z,
)
from src.editor.models import VoxelGrid


# ---------------------------------------------------------------------------
# Brush footprint tests
# ---------------------------------------------------------------------------


class TestBrushFootprint:
    def test_square_size_1_single_voxel(self):
        positions = get_brush_footprint(5, 5, BrushType.SQUARE, size=1)
        assert positions == [(5, 5)]

    def test_square_size_2_3x3(self):
        positions = get_brush_footprint(5, 5, BrushType.SQUARE, size=2)
        assert len(positions) == 9
        assert (5, 5) in positions
        assert (4, 4) in positions
        assert (6, 6) in positions

    def test_square_size_3_5x5(self):
        positions = get_brush_footprint(5, 5, BrushType.SQUARE, size=3)
        assert len(positions) == 25

    def test_circle_size_1_single_voxel(self):
        positions = get_brush_footprint(5, 5, BrushType.CIRCLE, size=1)
        assert positions == [(5, 5)]

    def test_circle_size_2_excludes_corners(self):
        """Circle of size 2 (radius 1) should include all 9 positions
        since distance from center to corner is sqrt(2) ≈ 1.41 < 1.5."""
        positions = get_brush_footprint(5, 5, BrushType.CIRCLE, size=2)
        # With radius=1 and threshold of 1.5, all 9 positions fit
        assert (5, 5) in positions
        assert (4, 5) in positions
        assert (5, 4) in positions

    def test_circle_larger_excludes_far_corners(self):
        """Circle of size 4 (radius 3) should exclude distant corners."""
        positions = get_brush_footprint(5, 5, BrushType.CIRCLE, size=4)
        # Corner at (2, 2) relative offset = (-3, -3), distance = sqrt(18) ≈ 4.24 > 3.5
        assert (2, 2) not in positions
        # But axis-aligned edges should be included
        assert (2, 5) in positions  # offset (-3, 0), distance = 3 < 3.5
        assert (5, 2) in positions


# ---------------------------------------------------------------------------
# Raycast placement tests
# ---------------------------------------------------------------------------


class TestRaycastPlacement:
    def test_empty_column_places_at_z0(self):
        """When no voxels exist at (x,y), should place at z=0 (back plane)."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        z = _raycast_for_placement_z(grid, 4, 4)
        assert z == 0

    def test_places_in_front_of_existing_voxel(self):
        """When a voxel exists at z=3, new voxel should go at z=4."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        grid.set(4, 4, 3, 1)
        z = _raycast_for_placement_z(grid, 4, 4)
        assert z == 4

    def test_places_in_front_of_frontmost_voxel(self):
        """When multiple voxels exist, place in front of the frontmost one."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        grid.set(4, 4, 0, 1)  # back
        grid.set(4, 4, 3, 1)  # middle
        grid.set(4, 4, 5, 1)  # front
        z = _raycast_for_placement_z(grid, 4, 4)
        assert z == 6  # in front of z=5

    def test_clamps_at_grid_boundary(self):
        """When frontmost voxel is at max z, can't place further front."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        grid.set(4, 4, 7, 1)  # at depth-1 (max)
        z = _raycast_for_placement_z(grid, 4, 4)
        # Should return 7 (the hit position itself, since z=8 is out of bounds)
        assert z == 7

    def test_different_columns_independent(self):
        """Raycast at different (x,y) positions should be independent."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        grid.set(2, 2, 5, 1)
        grid.set(4, 4, 1, 1)
        assert _raycast_for_placement_z(grid, 2, 2) == 6
        assert _raycast_for_placement_z(grid, 4, 4) == 2
        assert _raycast_for_placement_z(grid, 6, 6) == 0  # empty


# ---------------------------------------------------------------------------
# Core bug fix: fill_all_depths=False should NOT use back layer
# ---------------------------------------------------------------------------


class TestFillAllDepthsDisabled:
    """These tests verify the fix for the core bug:
    When fill_all_depths=False, 2D brushes should raycast and place voxels
    in front of existing surfaces, NOT always on the back layer (z=0).
    """

    def test_add_voxel_in_front_of_surface_not_at_back(self):
        """CORE BUG FIX: Adding voxels should go in front of existing ones,
        not at the back layer."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        # Place existing surface at z=3
        grid.set(4, 4, 3, 1)

        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=1,
            fill_all_depths=False,
            voxel_value=2,
        )
        result = apply_brush(grid, 4, 4, config)

        # Should have added at z=4 (in front of z=3), NOT at z=0
        assert len(result.added) == 1
        assert result.added[0] == (4, 4, 4)
        assert grid.get(4, 4, 4) == 2
        # Verify it did NOT add at z=0
        assert grid.get(4, 4, 0) is None

    def test_add_on_empty_grid_uses_back_plane(self):
        """When no voxels exist, fall back to z=0 (back plane)."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=1,
            fill_all_depths=False,
            voxel_value=1,
        )
        result = apply_brush(grid, 4, 4, config)
        assert len(result.added) == 1
        assert result.added[0] == (4, 4, 0)

    def test_square_brush_raycasts_per_column(self):
        """Each (x,y) in the brush should independently raycast for z-depth."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        # Create a surface with varying depths
        grid.set(3, 3, 2, 1)
        grid.set(4, 3, 4, 1)
        grid.set(3, 4, 1, 1)
        # (4, 4) is empty

        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=2,  # 3x3 brush centered at (4, 4) → covers (3,3) to (5,5)
            fill_all_depths=False,
            voxel_value=2,
        )
        result = apply_brush(grid, 4, 4, config)

        # Check that each column got the right z-depth
        added_dict = {(x, y): z for x, y, z in result.added}
        assert added_dict.get((3, 3)) == 3  # in front of z=2
        assert added_dict.get((4, 3)) == 5  # in front of z=4
        assert added_dict.get((3, 4)) == 2  # in front of z=1
        assert added_dict.get((4, 4)) == 0  # empty column → back plane

    def test_circle_brush_raycasts_per_column(self):
        """Circle brush should also raycast per-column, not use back layer."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        grid.set(4, 4, 5, 1)  # center has voxel at z=5

        config = BrushConfig(
            brush_type=BrushType.CIRCLE,
            size=1,
            fill_all_depths=False,
            voxel_value=2,
        )
        result = apply_brush(grid, 4, 4, config)

        assert len(result.added) == 1
        assert result.added[0] == (4, 4, 6)  # in front of z=5

    def test_stacking_multiple_brush_strokes(self):
        """Multiple brush strokes should stack voxels upward."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=1,
            fill_all_depths=False,
            voxel_value=1,
        )

        # First stroke: empty grid → z=0
        result1 = apply_brush(grid, 4, 4, config)
        assert result1.added[0] == (4, 4, 0)

        # Second stroke: in front of z=0 → z=1
        result2 = apply_brush(grid, 4, 4, config)
        assert result2.added[0] == (4, 4, 1)

        # Third stroke: in front of z=1 → z=2
        result3 = apply_brush(grid, 4, 4, config)
        assert result3.added[0] == (4, 4, 2)

    def test_remove_frontmost_voxel(self):
        """When removing with fill_all_depths=False, remove the frontmost voxel."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        grid.set(4, 4, 0, 1)
        grid.set(4, 4, 3, 1)
        grid.set(4, 4, 5, 1)

        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=1,
            fill_all_depths=False,
            operation=BrushOperation.REMOVE,
        )
        result = apply_brush(grid, 4, 4, config)

        assert len(result.removed) == 1
        assert result.removed[0] == (4, 4, 5)  # frontmost removed
        assert grid.has_voxel(4, 4, 0)  # back still there
        assert grid.has_voxel(4, 4, 3)  # middle still there

    def test_does_not_add_to_occupied_position(self):
        """If the placement position is already occupied, skip it."""
        grid = VoxelGrid(width=8, height=8, depth=8)
        grid.set(4, 4, 7, 1)  # at max depth

        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=1,
            fill_all_depths=False,
            voxel_value=2,
        )
        result = apply_brush(grid, 4, 4, config)
        # z=8 is out of bounds, falls back to z=7 which is occupied → nothing added
        assert len(result.added) == 0


# ---------------------------------------------------------------------------
# fill_all_depths=True tests (existing behavior, ensure no regression)
# ---------------------------------------------------------------------------


class TestFillAllDepthsEnabled:
    def test_fills_entire_depth_column(self):
        grid = VoxelGrid(width=4, height=4, depth=4)
        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=1,
            fill_all_depths=True,
            voxel_value=1,
        )
        result = apply_brush(grid, 2, 2, config)

        assert len(result.added) == 4  # all 4 z-depths
        for z in range(4):
            assert grid.has_voxel(2, 2, z)

    def test_skips_already_filled_positions(self):
        grid = VoxelGrid(width=4, height=4, depth=4)
        grid.set(2, 2, 1, 99)  # pre-existing

        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=1,
            fill_all_depths=True,
            voxel_value=1,
        )
        result = apply_brush(grid, 2, 2, config)

        assert len(result.added) == 3  # skipped z=1
        assert grid.get(2, 2, 1) == 99  # preserved original value

    def test_remove_all_depths(self):
        grid = VoxelGrid(width=4, height=4, depth=4)
        for z in range(4):
            grid.set(2, 2, z, 1)

        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=1,
            fill_all_depths=True,
            operation=BrushOperation.REMOVE,
        )
        result = apply_brush(grid, 2, 2, config)

        assert len(result.removed) == 4
        for z in range(4):
            assert not grid.has_voxel(2, 2, z)

    def test_large_square_brush_fills_all(self):
        grid = VoxelGrid(width=8, height=8, depth=4)
        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=2,  # 3x3
            fill_all_depths=True,
            voxel_value=1,
        )
        result = apply_brush(grid, 4, 4, config)

        # 3x3 footprint × 4 depths = 36 voxels
        assert len(result.added) == 36


# ---------------------------------------------------------------------------
# Edge cases and bounds checking
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_brush_at_grid_edge_clips(self):
        """Brush positions outside grid bounds should be clipped."""
        grid = VoxelGrid(width=4, height=4, depth=4)
        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=2,  # 3x3 centered at (0,0) → some positions negative
            fill_all_depths=False,
            voxel_value=1,
        )
        result = apply_brush(grid, 0, 0, config)

        # Only (0,0), (1,0), (0,1), (1,1) are in bounds
        assert len(result.added) == 4
        for x, y, z in result.added:
            assert 0 <= x < 4
            assert 0 <= y < 4

    def test_brush_completely_outside_grid(self):
        grid = VoxelGrid(width=4, height=4, depth=4)
        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=1,
            fill_all_depths=False,
            voxel_value=1,
        )
        result = apply_brush(grid, -5, -5, config)
        assert len(result.added) == 0

    def test_remove_from_empty_grid(self):
        grid = VoxelGrid(width=4, height=4, depth=4)
        config = BrushConfig(
            brush_type=BrushType.SQUARE,
            size=1,
            fill_all_depths=False,
            operation=BrushOperation.REMOVE,
        )
        result = apply_brush(grid, 2, 2, config)
        assert len(result.removed) == 0
