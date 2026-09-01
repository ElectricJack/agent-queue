from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_graph_identity import PlaybookGraphIdentity
    from ..models.playbook_graph_layout import PlaybookGraphLayout
    from ..models.playbook_graph_nodes_edges import PlaybookGraphNodesEdges
    from ..models.playbook_graph_view_response_legend import PlaybookGraphViewResponseLegend
    from ..models.playbook_graph_view_response_live_state_type_0 import PlaybookGraphViewResponseLiveStateType0
    from ..models.playbook_graph_view_response_node_metrics_type_0 import PlaybookGraphViewResponseNodeMetricsType0
    from ..models.playbook_graph_view_response_run_history_type_0_item import (
        PlaybookGraphViewResponseRunHistoryType0Item,
    )
    from ..models.playbook_graph_view_response_run_overlay_type_0 import PlaybookGraphViewResponseRunOverlayType0


T = TypeVar("T", bound="PlaybookGraphViewResponse")


@_attrs_define
class PlaybookGraphViewResponse:
    """``build_graph_view`` output — the nested shape the builder actually
    produces (design spec §4).

    The overlay blocks (``live_state``, ``run_overlay``, ``run_history``,
    ``node_metrics``) stay loosely typed: they are opt-in, richly dynamic,
    and not part of the first Graph tab.  They are declared here so the
    response model never silently drops them.

        Attributes:
            playbook (PlaybookGraphIdentity): Identity block of the graph view — the compiled playbook itself.
            success (bool | Unset):  Default: True.
            graph (PlaybookGraphNodesEdges | Unset):
            layout (PlaybookGraphLayout | Unset):
            legend (PlaybookGraphViewResponseLegend | Unset):
            live_state (None | PlaybookGraphViewResponseLiveStateType0 | Unset):
            run_overlay (None | PlaybookGraphViewResponseRunOverlayType0 | Unset):
            run_history (list[PlaybookGraphViewResponseRunHistoryType0Item] | None | Unset):
            node_metrics (None | PlaybookGraphViewResponseNodeMetricsType0 | Unset):
    """

    playbook: PlaybookGraphIdentity
    success: bool | Unset = True
    graph: PlaybookGraphNodesEdges | Unset = UNSET
    layout: PlaybookGraphLayout | Unset = UNSET
    legend: PlaybookGraphViewResponseLegend | Unset = UNSET
    live_state: None | PlaybookGraphViewResponseLiveStateType0 | Unset = UNSET
    run_overlay: None | PlaybookGraphViewResponseRunOverlayType0 | Unset = UNSET
    run_history: list[PlaybookGraphViewResponseRunHistoryType0Item] | None | Unset = UNSET
    node_metrics: None | PlaybookGraphViewResponseNodeMetricsType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.playbook_graph_view_response_live_state_type_0 import (
            PlaybookGraphViewResponseLiveStateType0,  # noqa: PLC0415
        )
        from ..models.playbook_graph_view_response_node_metrics_type_0 import (
            PlaybookGraphViewResponseNodeMetricsType0,  # noqa: PLC0415
        )
        from ..models.playbook_graph_view_response_run_overlay_type_0 import (
            PlaybookGraphViewResponseRunOverlayType0,  # noqa: PLC0415
        )

        playbook = self.playbook.to_dict()

        success = self.success

        graph: dict[str, Any] | Unset = UNSET
        if not isinstance(self.graph, Unset):
            graph = self.graph.to_dict()

        layout: dict[str, Any] | Unset = UNSET
        if not isinstance(self.layout, Unset):
            layout = self.layout.to_dict()

        legend: dict[str, Any] | Unset = UNSET
        if not isinstance(self.legend, Unset):
            legend = self.legend.to_dict()

        live_state: dict[str, Any] | None | Unset
        if isinstance(self.live_state, Unset):
            live_state = UNSET
        elif isinstance(self.live_state, PlaybookGraphViewResponseLiveStateType0):
            live_state = self.live_state.to_dict()
        else:
            live_state = self.live_state

        run_overlay: dict[str, Any] | None | Unset
        if isinstance(self.run_overlay, Unset):
            run_overlay = UNSET
        elif isinstance(self.run_overlay, PlaybookGraphViewResponseRunOverlayType0):
            run_overlay = self.run_overlay.to_dict()
        else:
            run_overlay = self.run_overlay

        run_history: list[dict[str, Any]] | None | Unset
        if isinstance(self.run_history, Unset):
            run_history = UNSET
        elif isinstance(self.run_history, list):
            run_history = []
            for run_history_type_0_item_data in self.run_history:
                run_history_type_0_item = run_history_type_0_item_data.to_dict()
                run_history.append(run_history_type_0_item)

        else:
            run_history = self.run_history

        node_metrics: dict[str, Any] | None | Unset
        if isinstance(self.node_metrics, Unset):
            node_metrics = UNSET
        elif isinstance(self.node_metrics, PlaybookGraphViewResponseNodeMetricsType0):
            node_metrics = self.node_metrics.to_dict()
        else:
            node_metrics = self.node_metrics

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook": playbook,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if graph is not UNSET:
            field_dict["graph"] = graph
        if layout is not UNSET:
            field_dict["layout"] = layout
        if legend is not UNSET:
            field_dict["legend"] = legend
        if live_state is not UNSET:
            field_dict["live_state"] = live_state
        if run_overlay is not UNSET:
            field_dict["run_overlay"] = run_overlay
        if run_history is not UNSET:
            field_dict["run_history"] = run_history
        if node_metrics is not UNSET:
            field_dict["node_metrics"] = node_metrics

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_graph_identity import PlaybookGraphIdentity  # noqa: PLC0415
        from ..models.playbook_graph_layout import PlaybookGraphLayout  # noqa: PLC0415
        from ..models.playbook_graph_nodes_edges import PlaybookGraphNodesEdges  # noqa: PLC0415
        from ..models.playbook_graph_view_response_legend import PlaybookGraphViewResponseLegend  # noqa: PLC0415
        from ..models.playbook_graph_view_response_live_state_type_0 import (
            PlaybookGraphViewResponseLiveStateType0,  # noqa: PLC0415
        )
        from ..models.playbook_graph_view_response_node_metrics_type_0 import (
            PlaybookGraphViewResponseNodeMetricsType0,  # noqa: PLC0415
        )
        from ..models.playbook_graph_view_response_run_history_type_0_item import (
            PlaybookGraphViewResponseRunHistoryType0Item,  # noqa: PLC0415
        )
        from ..models.playbook_graph_view_response_run_overlay_type_0 import (
            PlaybookGraphViewResponseRunOverlayType0,  # noqa: PLC0415
        )

        d = dict(src_dict)
        playbook = PlaybookGraphIdentity.from_dict(d.pop("playbook"))

        success = d.pop("success", UNSET)

        _graph = d.pop("graph", UNSET)
        graph: PlaybookGraphNodesEdges | Unset
        if isinstance(_graph, Unset):
            graph = UNSET
        else:
            graph = PlaybookGraphNodesEdges.from_dict(_graph)

        _layout = d.pop("layout", UNSET)
        layout: PlaybookGraphLayout | Unset
        if isinstance(_layout, Unset):
            layout = UNSET
        else:
            layout = PlaybookGraphLayout.from_dict(_layout)

        _legend = d.pop("legend", UNSET)
        legend: PlaybookGraphViewResponseLegend | Unset
        if isinstance(_legend, Unset):
            legend = UNSET
        else:
            legend = PlaybookGraphViewResponseLegend.from_dict(_legend)

        def _parse_live_state(data: object) -> None | PlaybookGraphViewResponseLiveStateType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                live_state_type_0 = PlaybookGraphViewResponseLiveStateType0.from_dict(data)

                return live_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookGraphViewResponseLiveStateType0 | Unset, data)

        live_state = _parse_live_state(d.pop("live_state", UNSET))

        def _parse_run_overlay(data: object) -> None | PlaybookGraphViewResponseRunOverlayType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                run_overlay_type_0 = PlaybookGraphViewResponseRunOverlayType0.from_dict(data)

                return run_overlay_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookGraphViewResponseRunOverlayType0 | Unset, data)

        run_overlay = _parse_run_overlay(d.pop("run_overlay", UNSET))

        def _parse_run_history(data: object) -> list[PlaybookGraphViewResponseRunHistoryType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                run_history_type_0 = []
                _run_history_type_0 = data
                for run_history_type_0_item_data in _run_history_type_0:
                    run_history_type_0_item = PlaybookGraphViewResponseRunHistoryType0Item.from_dict(
                        run_history_type_0_item_data
                    )

                    run_history_type_0.append(run_history_type_0_item)

                return run_history_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[PlaybookGraphViewResponseRunHistoryType0Item] | None | Unset, data)

        run_history = _parse_run_history(d.pop("run_history", UNSET))

        def _parse_node_metrics(data: object) -> None | PlaybookGraphViewResponseNodeMetricsType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                node_metrics_type_0 = PlaybookGraphViewResponseNodeMetricsType0.from_dict(data)

                return node_metrics_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookGraphViewResponseNodeMetricsType0 | Unset, data)

        node_metrics = _parse_node_metrics(d.pop("node_metrics", UNSET))

        playbook_graph_view_response = cls(
            playbook=playbook,
            success=success,
            graph=graph,
            layout=layout,
            legend=legend,
            live_state=live_state,
            run_overlay=run_overlay,
            run_history=run_history,
            node_metrics=node_metrics,
        )

        playbook_graph_view_response.additional_properties = d
        return playbook_graph_view_response

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
