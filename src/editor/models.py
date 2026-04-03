"""Core data models for the level editor.

Defines the data structures for the layer/page/voxel system:
- VoxelGrid: 3D grid of voxel data within a page
- Page: Reusable content template (flyweight pattern)
- PageInstance: Lightweight placement reference
- Layer: Depth plane containing page instances
- Level: Top-level container
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class VoxelGrid:
    """A 3D grid of voxels within a page.

    Coordinates are (x, y, z) where:
    - x: horizontal position
    - y: vertical position
    - z: depth (0 = back, max = front/closest to camera)
    """

    width: int = 16
    height: int = 16
    depth: int = 16
    # Sparse storage: (x, y, z) -> voxel_value (material ID, color, etc.)
    voxels: dict[tuple[int, int, int], int] = field(default_factory=dict)

    def get(self, x: int, y: int, z: int) -> Optional[int]:
        """Get voxel value at position, or None if empty."""
        return self.voxels.get((x, y, z))

    def set(self, x: int, y: int, z: int, value: int) -> None:
        """Set a voxel at the given position."""
        if not (0 <= x < self.width and 0 <= y < self.height and 0 <= z < self.depth):
            raise ValueError(
                f"Position ({x}, {y}, {z}) out of bounds for "
                f"grid {self.width}x{self.height}x{self.depth}"
            )
        self.voxels[(x, y, z)] = value

    def remove(self, x: int, y: int, z: int) -> Optional[int]:
        """Remove and return voxel at position, or None if empty."""
        return self.voxels.pop((x, y, z), None)

    def has_voxel(self, x: int, y: int, z: int) -> bool:
        """Check if a voxel exists at the given position."""
        return (x, y, z) in self.voxels

    def get_front_voxel_z(self, x: int, y: int) -> Optional[int]:
        """Find the frontmost (highest z) occupied voxel at (x, y).

        Returns the z coordinate of the frontmost voxel, or None if
        the column is empty.
        """
        max_z = None
        for vx, vy, vz in self.voxels:
            if vx == x and vy == y:
                if max_z is None or vz > max_z:
                    max_z = vz
        return max_z

    def get_back_voxel_z(self, x: int, y: int) -> Optional[int]:
        """Find the backmost (lowest z) occupied voxel at (x, y).

        Returns the z coordinate of the backmost voxel, or None if
        the column is empty.
        """
        min_z = None
        for vx, vy, vz in self.voxels:
            if vx == x and vy == y:
                if min_z is None or vz < min_z:
                    min_z = vz
        return min_z

    def raycast_z(self, x: int, y: int, from_front: bool = True) -> Optional[int]:
        """Cast a ray along the z-axis at (x, y) and find the first hit.

        Args:
            x: X coordinate to cast through
            y: Y coordinate to cast through
            from_front: If True, cast from front (high z) toward back (low z).
                       If False, cast from back toward front.

        Returns:
            The z coordinate of the first voxel hit, or None if no hit.
        """
        if from_front:
            return self.get_front_voxel_z(x, y)
        else:
            return self.get_back_voxel_z(x, y)


@dataclass
class Page:
    """A reusable content template. All instances share this state."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    width: int = 256
    height: int = 256
    content: dict = field(default_factory=dict)
    voxel_grid: VoxelGrid = field(default_factory=VoxelGrid)

    def clone(self) -> Page:
        """Create an independent copy (breaks instance sharing)."""
        return Page(
            id=str(uuid.uuid4()),
            name=f"{self.name} (copy)",
            width=self.width,
            height=self.height,
            content=copy.deepcopy(self.content),
            voxel_grid=copy.deepcopy(self.voxel_grid),
        )


@dataclass
class PageInstance:
    """A placement of a Page within a Layer at a specific grid position."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    page_id: str = ""
    grid_x: int = 0
    grid_y: int = 0


@dataclass
class Layer:
    """A depth plane containing page instances."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    z_depth: float = 0.0
    visible: bool = True
    locked: bool = False
    opacity: float = 1.0
    page_instances: list[PageInstance] = field(default_factory=list)

    def add_page(self, page_id: str, grid_x: int, grid_y: int) -> PageInstance:
        """Place a page instance at the given grid position."""
        for inst in self.page_instances:
            if inst.grid_x == grid_x and inst.grid_y == grid_y:
                raise ValueError(
                    f"Grid position ({grid_x}, {grid_y}) already occupied "
                    f"by page instance {inst.id}"
                )
        instance = PageInstance(page_id=page_id, grid_x=grid_x, grid_y=grid_y)
        self.page_instances.append(instance)
        return instance

    def remove_page_at(self, grid_x: int, grid_y: int) -> Optional[PageInstance]:
        """Remove and return the page instance at the given grid position."""
        for i, inst in enumerate(self.page_instances):
            if inst.grid_x == grid_x and inst.grid_y == grid_y:
                return self.page_instances.pop(i)
        return None


@dataclass
class Level:
    """Top-level container for all layers and the page library."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    page_library: dict[str, Page] = field(default_factory=dict)
    layers: list[Layer] = field(default_factory=list)
    cell_width: int = 256
    cell_height: int = 256

    def add_layer(self, name: str, z_depth: Optional[float] = None) -> Layer:
        """Add a new layer. Auto-assigns z_depth if not specified."""
        if z_depth is None:
            z_depth = self._next_z_depth()
        if any(lay.z_depth == z_depth for lay in self.layers):
            raise ValueError(f"z_depth {z_depth} already in use")
        layer = Layer(name=name, z_depth=z_depth)
        self.layers.append(layer)
        self._sort_layers()
        return layer

    def reorder_layer(self, layer_id: str, new_z_depth: float) -> None:
        """Move a layer to a new z-depth."""
        if any(lay.z_depth == new_z_depth and lay.id != layer_id for lay in self.layers):
            raise ValueError(f"z_depth {new_z_depth} already in use")
        for layer in self.layers:
            if layer.id == layer_id:
                layer.z_depth = new_z_depth
                break
        self._sort_layers()

    def _next_z_depth(self) -> float:
        if not self.layers:
            return 0.0
        return max(lay.z_depth for lay in self.layers) + 1.0

    def _sort_layers(self) -> None:
        self.layers.sort(key=lambda lay: lay.z_depth)

    def register_page(self, page: Page) -> None:
        self.page_library[page.id] = page

    def get_page(self, page_id: str) -> Optional[Page]:
        return self.page_library.get(page_id)
