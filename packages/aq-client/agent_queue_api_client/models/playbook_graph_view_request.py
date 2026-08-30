from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookGraphViewRequest")


@_attrs_define
class PlaybookGraphViewRequest:
    """
    Attributes:
        playbook_id (str): The playbook identifier to visualize.
        direction (str | Unset): Layout direction: 'TD' (top-down) or 'LR' (left-right). Default: TD. Default: 'TD'.
        show_prompts (bool | Unset): Include truncated prompt previews in node labels. Default: true. Default: True.
        run_id (None | str | Unset): Overlay a specific run's path on the graph. Shows which nodes were visited, timing,
            and token usage per node.
        include_live_state (bool | Unset): Include live state overlay for running/paused instances. Highlights the
            current node. Default: true. Default: True.
        include_metrics (bool | Unset): Include per-node health metrics overlay (failure rate, avg duration, token
            usage). Default: false. Default: False.
        include_history (bool | Unset): Include run history timeline showing past runs and paths taken. Default: false.
            Default: False.
        history_limit (int | Unset): Max runs in the history timeline (default 20). Default: 20.
    """

    playbook_id: str
    direction: str | Unset = "TD"
    show_prompts: bool | Unset = True
    run_id: None | str | Unset = UNSET
    include_live_state: bool | Unset = True
    include_metrics: bool | Unset = False
    include_history: bool | Unset = False
    history_limit: int | Unset = 20
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        direction = self.direction

        show_prompts = self.show_prompts

        run_id: None | str | Unset
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id

        include_live_state = self.include_live_state

        include_metrics = self.include_metrics

        include_history = self.include_history

        history_limit = self.history_limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
            }
        )
        if direction is not UNSET:
            field_dict["direction"] = direction
        if show_prompts is not UNSET:
            field_dict["show_prompts"] = show_prompts
        if run_id is not UNSET:
            field_dict["run_id"] = run_id
        if include_live_state is not UNSET:
            field_dict["include_live_state"] = include_live_state
        if include_metrics is not UNSET:
            field_dict["include_metrics"] = include_metrics
        if include_history is not UNSET:
            field_dict["include_history"] = include_history
        if history_limit is not UNSET:
            field_dict["history_limit"] = history_limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        direction = d.pop("direction", UNSET)

        show_prompts = d.pop("show_prompts", UNSET)

        def _parse_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))

        include_live_state = d.pop("include_live_state", UNSET)

        include_metrics = d.pop("include_metrics", UNSET)

        include_history = d.pop("include_history", UNSET)

        history_limit = d.pop("history_limit", UNSET)

        playbook_graph_view_request = cls(
            playbook_id=playbook_id,
            direction=direction,
            show_prompts=show_prompts,
            run_id=run_id,
            include_live_state=include_live_state,
            include_metrics=include_metrics,
            include_history=include_history,
            history_limit=history_limit,
        )

        playbook_graph_view_request.additional_properties = d
        return playbook_graph_view_request

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
