from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookV2GraphRequest")


@_attrs_define
class PlaybookV2GraphRequest:
    """
    Attributes:
        playbook_id (str): The playbook identifier to project.
        artifact_sha256 (None | str | Unset): Project this exact artifact instead of the active one. Full 'sha256:<64
            hex>' form.
        event_type (None | str | Unset): Narrow rules/nodes/edges to the rules this event triggers. event_groups still
            lists every event and no reachable branch is dropped.
        direction (str | Unset): Layout direction: 'TD' (top-down) or 'LR' (left-right). Default: TD. Default: 'TD'.
        include_advanced (bool | Unset): Include the canonical typed step body in advanced.typed_step. Default: true;
            false leaves the field present but empty so the response type never changes. Default: True.
    """

    playbook_id: str
    artifact_sha256: None | str | Unset = UNSET
    event_type: None | str | Unset = UNSET
    direction: str | Unset = "TD"
    include_advanced: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        artifact_sha256: None | str | Unset
        if isinstance(self.artifact_sha256, Unset):
            artifact_sha256 = UNSET
        else:
            artifact_sha256 = self.artifact_sha256

        event_type: None | str | Unset
        if isinstance(self.event_type, Unset):
            event_type = UNSET
        else:
            event_type = self.event_type

        direction = self.direction

        include_advanced = self.include_advanced

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
            }
        )
        if artifact_sha256 is not UNSET:
            field_dict["artifact_sha256"] = artifact_sha256
        if event_type is not UNSET:
            field_dict["event_type"] = event_type
        if direction is not UNSET:
            field_dict["direction"] = direction
        if include_advanced is not UNSET:
            field_dict["include_advanced"] = include_advanced

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        def _parse_artifact_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        artifact_sha256 = _parse_artifact_sha256(d.pop("artifact_sha256", UNSET))

        def _parse_event_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        event_type = _parse_event_type(d.pop("event_type", UNSET))

        direction = d.pop("direction", UNSET)

        include_advanced = d.pop("include_advanced", UNSET)

        playbook_v2_graph_request = cls(
            playbook_id=playbook_id,
            artifact_sha256=artifact_sha256,
            event_type=event_type,
            direction=direction,
            include_advanced=include_advanced,
        )

        playbook_v2_graph_request.additional_properties = d
        return playbook_v2_graph_request

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
