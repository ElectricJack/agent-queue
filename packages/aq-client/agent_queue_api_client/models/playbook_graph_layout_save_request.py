from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.playbook_graph_layout_save_request_positions import PlaybookGraphLayoutSaveRequestPositions


T = TypeVar("T", bound="PlaybookGraphLayoutSaveRequest")


@_attrs_define
class PlaybookGraphLayoutSaveRequest:
    """
    Attributes:
        playbook_id (str):
        artifact_sha256 (str):
        positions (PlaybookGraphLayoutSaveRequestPositions):
    """

    playbook_id: str
    artifact_sha256: str
    positions: PlaybookGraphLayoutSaveRequestPositions
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        artifact_sha256 = self.artifact_sha256

        positions = self.positions.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "artifact_sha256": artifact_sha256,
                "positions": positions,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_graph_layout_save_request_positions import PlaybookGraphLayoutSaveRequestPositions

        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        artifact_sha256 = d.pop("artifact_sha256")

        positions = PlaybookGraphLayoutSaveRequestPositions.from_dict(d.pop("positions"))

        playbook_graph_layout_save_request = cls(
            playbook_id=playbook_id,
            artifact_sha256=artifact_sha256,
            positions=positions,
        )

        playbook_graph_layout_save_request.additional_properties = d
        return playbook_graph_layout_save_request

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
