"""Brush system for the voxel level editor.

Handles 2D brush shapes (square, circle) with correct z-depth placement
based on raycasting when fill_all_depths is disabled.

Key behavior:
- When fill_all_depths=True: brush fills all z-depths in the grid
- When fill_all_depths=False: brush raycasts from the camera direction to find
  the first intersected voxel, then places new voxels IN FRONT of that surface.
  If no voxel is hit, falls back to the back-most plane (z=0).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .models import VoxelGrid


class BrushType(Enum):
    """Supported 2D brush shapes."""

    SQUARE = "square"
    CIRCLE = "circle"


class BrushOperation(Enum):
    """What the brush does when applied."""

    ADD = "add"
    REMOVE = "remove"


@dataclass
class BrushConfig:
    """Configuration for a brush stroke."""

    brush_type: BrushType = BrushType.SQUARE
    operation: BrushOperation = BrushOperation.ADD
    size: int = 1  # radius in voxels (1 = single voxel, 2 = 3x3, etc.)
    fill_all_depths: bool = True
    voxel_value: int = 1  # material/color ID to place


@dataclass
class BrushResult:
    """Result of applying a brush stroke."""

    added: list[tuple[int, int, int]] = field(default_factory=list)
    removed: list[tuple[int, int, int]] = field(default_factory=list)


def get_brush_footprint(
    center_x: int,
    center_y: int,
    brush_type: BrushType,
    size: int,
) -> list[tuple[int, int]]:
    """Compute the 2D (x, y) footprint of a brush centered at (center_x, center_y).

    Args:
        center_x: Center X coordinate
        center_y: Center Y coordinate
        brush_type: Shape of the brush
        size: Brush radius (1 = single voxel, 2 = 3x3 square / diameter-3 circle)

    Returns:
        List of (x, y) positions covered by the brush.
    """
    positions = []
    radius = size - 1  # size=1 means just the center, size=2 means radius of 1

    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            if brush_type == BrushType.CIRCLE:
                # Use Euclidean distance for circle shape
                # Add 0.5 to allow slightly larger circles at boundaries
                if math.sqrt(dx * dx + dy * dy) > radius + 0.5:
                    continue
            positions.append((center_x + dx, center_y + dy))

    return positions


def _raycast_for_placement_z(
    grid: VoxelGrid,
    x: int,
    y: int,
) -> int:
    """Determine the z-depth at which to place a new voxel at (x, y).

    Raycasts from the front (camera direction) toward the back to find
    the first existing voxel. Returns the z in front of that voxel
    (i.e., hit_z + 1) so the new voxel is placed on the visible surface.

    If no voxel is hit along the ray, falls back to the back-most plane (z=0).

    Args:
        grid: The voxel grid to raycast into
        x: X coordinate to cast through
        y: Y coordinate to cast through

    Returns:
        The z coordinate where the new voxel should be placed.
    """
    # Cast from front toward back to find the first (frontmost) voxel
    hit_z = grid.raycast_z(x, y, from_front=True)

    if hit_z is not None:
        # Place in front of the hit voxel
        placement_z = hit_z + 1
        # Clamp to grid bounds
        if placement_z >= grid.depth:
            # Surface is at the very front — can't place in front of it
            # Return the hit position itself (will be skipped if occupied)
            return hit_z
        return placement_z
    else:
        # No voxel hit — use the back-most plane as fallback
        return 0


def _raycast_for_removal_z(
    grid: VoxelGrid,
    x: int,
    y: int,
) -> Optional[int]:
    """Determine which voxel to remove at (x, y) when fill_all_depths is off.

    Raycasts from front to find the frontmost voxel to remove.

    Returns:
        The z coordinate of the voxel to remove, or None if no voxel exists.
    """
    return grid.raycast_z(x, y, from_front=True)


def apply_brush(
    grid: VoxelGrid,
    center_x: int,
    center_y: int,
    config: BrushConfig,
) -> BrushResult:
    """Apply a brush stroke to a voxel grid.

    This is the main entry point for brush operations. Handles both
    fill_all_depths=True and fill_all_depths=False modes.

    When fill_all_depths=False and adding voxels:
    - For each (x, y) in the brush footprint, raycast from the front
      to find the first existing voxel
    - Place the new voxel IN FRONT of the intersected voxel (hit_z + 1)
    - If no voxel is hit, place at the back-most plane (z=0)

    When fill_all_depths=True:
    - Fill all z-depths for each (x, y) in the brush footprint

    Args:
        grid: The voxel grid to modify
        center_x: Center X of the brush stroke
        center_y: Center Y of the brush stroke
        config: Brush configuration (type, size, operation, fill_all_depths)

    Returns:
        BrushResult with lists of added/removed voxel positions.
    """
    result = BrushResult()

    footprint = get_brush_footprint(center_x, center_y, config.brush_type, config.size)

    # Filter to positions within grid bounds (x, y only)
    valid_positions = [(x, y) for x, y in footprint if 0 <= x < grid.width and 0 <= y < grid.height]

    if config.operation == BrushOperation.ADD:
        _apply_add(grid, valid_positions, config, result)
    elif config.operation == BrushOperation.REMOVE:
        _apply_remove(grid, valid_positions, config, result)

    return result


def _apply_add(
    grid: VoxelGrid,
    positions: list[tuple[int, int]],
    config: BrushConfig,
    result: BrushResult,
) -> None:
    """Add voxels at the given 2D positions.

    When fill_all_depths=True: fills every z-level at each (x, y).
    When fill_all_depths=False: raycasts to find the correct z-depth
    and places voxels on the visible surface (in front of existing voxels).
    """
    if config.fill_all_depths:
        # Fill all z-depths
        for x, y in positions:
            for z in range(grid.depth):
                if not grid.has_voxel(x, y, z):
                    grid.set(x, y, z, config.voxel_value)
                    result.added.append((x, y, z))
    else:
        # Raycast to find correct z-depth for each (x, y)
        for x, y in positions:
            z = _raycast_for_placement_z(grid, x, y)
            if 0 <= z < grid.depth and not grid.has_voxel(x, y, z):
                grid.set(x, y, z, config.voxel_value)
                result.added.append((x, y, z))


def _apply_remove(
    grid: VoxelGrid,
    positions: list[tuple[int, int]],
    config: BrushConfig,
    result: BrushResult,
) -> None:
    """Remove voxels at the given 2D positions.

    When fill_all_depths=True: removes all voxels at each (x, y).
    When fill_all_depths=False: raycasts to find and remove only the
    frontmost voxel at each (x, y).
    """
    if config.fill_all_depths:
        # Remove all z-depths
        for x, y in positions:
            for z in range(grid.depth):
                removed = grid.remove(x, y, z)
                if removed is not None:
                    result.removed.append((x, y, z))
    else:
        # Raycast to find frontmost voxel to remove
        for x, y in positions:
            z = _raycast_for_removal_z(grid, x, y)
            if z is not None:
                grid.remove(x, y, z)
                result.removed.append((x, y, z))
