"""Response/request models for the layout endpoints (spatial-layout design §5).

These models back REST routes on ``src/api/graph_layout.py`` rather than
``CommandHandler`` commands, so — unlike the sibling category modules —
this one exposes no ``RESPONSE_MODELS`` mapping: the codegen/contract
machinery in :mod:`src.api.models` keys off command names, and there are
no commands here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.api.models.graph import GraphGate, GraphTaskNode

__all__ = [
    "AncestorRef",
    "ExtentResponse",
    "LayoutEdge",
    "LayoutJob",
    "LayoutNode",
    "LayoutRect",
    "LayoutStub",
    "LayoutWorker",
    "ListRequest",
    "ListResponse",
    "LocateHit",
    "LocateRequest",
    "LocateResponse",
    "NodeResponse",
    "StubOverflow",
    "TidyRequest",
    "TidyResponse",
    "TilesRequest",
    "TilesResponse",
]


class LayoutRect(BaseModel):
    x0: float
    y0: float
    x1: float
    y1: float


class TilesRequest(BaseModel):
    variant: str = "active"
    rect: LayoutRect
    expanded: list[str] = []
    root: str | None = None
    max_depth: int | None = None
    q: str = ""
    status: str = ""


class ListRequest(BaseModel):
    variant: str = "active"
    expanded: list[str] = []
    q: str = ""
    status: str = ""
    cursor: str | None = None
    limit: int = 50


class LocateRequest(BaseModel):
    """Where the matches for a filter are, in the geometry the canvas draws.

    Carries ``expanded`` for the same reason ``tiles`` and ``list`` are POSTs:
    collapsing a container reflows everything after it, so a match's position
    depends on the viewer's expanded set and cannot be answered from the
    persisted layout alone.
    """

    variant: str = "active"
    expanded: list[str] = []
    q: str = ""
    status: str = ""
    limit: int = 200


class LayoutJob(BaseModel):
    id: str
    project_id: str
    variant: str
    kind: str
    status: str
    requested_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


class ExtentResponse(BaseModel):
    layout_version: int
    extent_w: float
    extent_h: float
    node_count: int
    job: LayoutJob | None = None


class LayoutNode(GraphTaskNode):
    x: float
    y: float
    w: float
    h: float
    depth: int
    container_id: str | None = None
    kind: str
    context_only: bool = False
    agg_children: int = 0
    agg_descendants: int = 0
    agg_completed: int = 0
    agg_running: int = 0
    agg_blocked: int = 0
    agg_active: int = 0


class LayoutEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_task_id: str = Field(alias="from")
    to_task_id: str = Field(alias="to")
    dep_type: str
    description: str | None = None
    count: int = 1


class LayoutStub(BaseModel):
    id: str
    project_id: str
    x: float
    y: float
    w: float
    h: float
    title: str = ""


class StubOverflow(BaseModel):
    node_id: str
    direction: str
    more: int


class LayoutWorker(BaseModel):
    agent_id: str
    name: str
    docked_at: str
    in_collapsed: bool


class TilesResponse(BaseModel):
    nodes: list[LayoutNode] = []
    edges: list[LayoutEdge] = []
    stubs: list[LayoutStub] = []
    stub_overflow: list[StubOverflow] = []
    workers: list[LayoutWorker] = []
    gates: list[GraphGate] = []
    layout_version: int


class ListResponse(BaseModel):
    nodes: list[LayoutNode] = []
    next_cursor: str | None = None
    layout_version: int


class AncestorRef(BaseModel):
    id: str
    title: str
    x: float
    y: float
    w: float
    h: float


class NodeResponse(BaseModel):
    node: LayoutNode
    ancestors: list[AncestorRef] = []
    layout_version: int


class LocateHit(BaseModel):
    id: str
    x: float
    y: float
    w: float
    h: float
    container_id: str | None = None


class LocateResponse(BaseModel):
    hits: list[LocateHit] = []
    truncated: bool = False


class TidyRequest(BaseModel):
    variant: str | None = None


class TidyResponse(BaseModel):
    jobs: list[LayoutJob] = []
