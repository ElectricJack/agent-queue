from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_graph_layout_save_response_positions import PlaybookGraphLayoutSaveResponsePositions


T = TypeVar("T", bound="PlaybookGraphLayoutSaveResponse")


@_attrs_define
class PlaybookGraphLayoutSaveResponse:
    """
    Attributes:
        playbook_id (str):
        artifact_sha256 (str):
        success (bool | Unset):  Default: True.
        positions (PlaybookGraphLayoutSaveResponsePositions | Unset):
    """

    playbook_id: str
    artifact_sha256: str
    success: bool | Unset = True
    positions: PlaybookGraphLayoutSaveResponsePositions | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        artifact_sha256 = self.artifact_sha256

        success = self.success

        positions: dict[str, Any] | Unset = UNSET
        if not isinstance(self.positions, Unset):
            positions = self.positions.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "playbook_id": playbook_id,
                "artifact_sha256": artifact_sha256,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if positions is not UNSET:
            field_dict["positions"] = positions

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_graph_layout_save_response_positions import PlaybookGraphLayoutSaveResponsePositions

        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        artifact_sha256 = d.pop("artifact_sha256")

        success = d.pop("success", UNSET)

        _positions = d.pop("positions", UNSET)
        positions: PlaybookGraphLayoutSaveResponsePositions | Unset
        if isinstance(_positions, Unset):
            positions = UNSET
        else:
            positions = PlaybookGraphLayoutSaveResponsePositions.from_dict(_positions)

        playbook_graph_layout_save_response = cls(
            playbook_id=playbook_id,
            artifact_sha256=artifact_sha256,
            success=success,
            positions=positions,
        )

        return playbook_graph_layout_save_response
