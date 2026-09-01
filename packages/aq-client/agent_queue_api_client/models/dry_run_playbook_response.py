from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.dry_run_playbook_response_mock_event import DryRunPlaybookResponseMockEvent
    from ..models.dry_run_playbook_response_node_trace_item import DryRunPlaybookResponseNodeTraceItem


T = TypeVar("T", bound="DryRunPlaybookResponse")


@_attrs_define
class DryRunPlaybookResponse:
    """
    Attributes:
        playbook_id (str):
        status (str):
        dry_run (bool | Unset):  Default: True.
        version (int | Unset):  Default: 0.
        node_trace (list[DryRunPlaybookResponseNodeTraceItem] | Unset):
        node_count (int | Unset):  Default: 0.
        tokens_used (int | Unset):  Default: 0.
        mock_event (DryRunPlaybookResponseMockEvent | Unset):
    """

    playbook_id: str
    status: str
    dry_run: bool | Unset = True
    version: int | Unset = 0
    node_trace: list[DryRunPlaybookResponseNodeTraceItem] | Unset = UNSET
    node_count: int | Unset = 0
    tokens_used: int | Unset = 0
    mock_event: DryRunPlaybookResponseMockEvent | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        status = self.status

        dry_run = self.dry_run

        version = self.version

        node_trace: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.node_trace, Unset):
            node_trace = []
            for node_trace_item_data in self.node_trace:
                node_trace_item = node_trace_item_data.to_dict()
                node_trace.append(node_trace_item)

        node_count = self.node_count

        tokens_used = self.tokens_used

        mock_event: dict[str, Any] | Unset = UNSET
        if not isinstance(self.mock_event, Unset):
            mock_event = self.mock_event.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "status": status,
            }
        )
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if version is not UNSET:
            field_dict["version"] = version
        if node_trace is not UNSET:
            field_dict["node_trace"] = node_trace
        if node_count is not UNSET:
            field_dict["node_count"] = node_count
        if tokens_used is not UNSET:
            field_dict["tokens_used"] = tokens_used
        if mock_event is not UNSET:
            field_dict["mock_event"] = mock_event

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.dry_run_playbook_response_mock_event import DryRunPlaybookResponseMockEvent  # noqa: PLC0415
        from ..models.dry_run_playbook_response_node_trace_item import (
            DryRunPlaybookResponseNodeTraceItem,  # noqa: PLC0415
        )

        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        status = d.pop("status")

        dry_run = d.pop("dry_run", UNSET)

        version = d.pop("version", UNSET)

        _node_trace = d.pop("node_trace", UNSET)
        node_trace: list[DryRunPlaybookResponseNodeTraceItem] | Unset = UNSET
        if _node_trace is not UNSET:
            node_trace = []
            for node_trace_item_data in _node_trace:
                node_trace_item = DryRunPlaybookResponseNodeTraceItem.from_dict(node_trace_item_data)

                node_trace.append(node_trace_item)

        node_count = d.pop("node_count", UNSET)

        tokens_used = d.pop("tokens_used", UNSET)

        _mock_event = d.pop("mock_event", UNSET)
        mock_event: DryRunPlaybookResponseMockEvent | Unset
        if isinstance(_mock_event, Unset):
            mock_event = UNSET
        else:
            mock_event = DryRunPlaybookResponseMockEvent.from_dict(_mock_event)

        dry_run_playbook_response = cls(
            playbook_id=playbook_id,
            status=status,
            dry_run=dry_run,
            version=version,
            node_trace=node_trace,
            node_count=node_count,
            tokens_used=tokens_used,
            mock_event=mock_event,
        )

        dry_run_playbook_response.additional_properties = d
        return dry_run_playbook_response

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
