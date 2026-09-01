"""Engine data types (§4.1, §4.4 step 8)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SnapTask:
    id: str
    parent_id: str | None
    is_container: bool
    status: str
    created_at: float
    title: str = ""


@dataclass
class LayoutRow:
    task_id: str
    container_id: str | None
    path: str
    depth: int
    rank: int
    order_key: str
    w: float
    h: float
    rel_x: float
    rel_y: float
    abs_x: float
    abs_y: float
    kind: str  # card | container | stub
    agg_children: int = 0
    agg_descendants: int = 0
    agg_completed: int = 0
    agg_running: int = 0
    agg_blocked: int = 0
    agg_active: int = 0

    @property
    def ordinal(self) -> tuple[int, str]:
        return (self.rank, self.order_key)


@dataclass
class ContainerScope:
    """Everything the engine needs to lay out ONE container's children."""

    container_id: str | None  # None = project root
    container_path: str  # "/" for root, "/<a>/<b>/" otherwise
    depth: int  # depth of the children
    children: dict[str, SnapTask]
    existing: dict[str, LayoutRow]  # existing rows for children (by task_id)
    sibling_edges: list[tuple[str, str]]  # (dependent, blocker) among children
    child_sizes: dict[str, tuple[float, float]]  # allocated (w, h) for container children
    stub_ids: frozenset[str] = frozenset()  # container children rendered as stubs
    origin: tuple[float, float] = (0.0, 0.0)  # abs coords of container content origin


@dataclass
class Translation:
    path_prefix: str
    dx: float
    dy: float


@dataclass
class WriteSet:
    upserts: list[LayoutRow] = field(default_factory=list)
    deletes: list[str] = field(default_factory=list)  # task ids
    translations: list[Translation] = field(default_factory=list)
    # Allocated size of each re-laid container (task_id or ROOT) so the driver
    # can propagate to the parent scope.
    sizes: dict[str, tuple[float, float]] = field(default_factory=dict)
