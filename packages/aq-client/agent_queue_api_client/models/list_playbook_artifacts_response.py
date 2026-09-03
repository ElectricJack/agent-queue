from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_artifact_summary_dto import PlaybookArtifactSummaryDTO


T = TypeVar("T", bound="ListPlaybookArtifactsResponse")


@_attrs_define
class ListPlaybookArtifactsResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        playbook_id (str | Unset):  Default: ''.
        artifacts (list[PlaybookArtifactSummaryDTO] | Unset):
        count (int | Unset):  Default: 0.
        active_artifact_sha256 (None | str | Unset):
    """

    success: bool | Unset = True
    playbook_id: str | Unset = ""
    artifacts: list[PlaybookArtifactSummaryDTO] | Unset = UNSET
    count: int | Unset = 0
    active_artifact_sha256: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        playbook_id = self.playbook_id

        artifacts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.artifacts, Unset):
            artifacts = []
            for artifacts_item_data in self.artifacts:
                artifacts_item = artifacts_item_data.to_dict()
                artifacts.append(artifacts_item)

        count = self.count

        active_artifact_sha256: None | str | Unset
        if isinstance(self.active_artifact_sha256, Unset):
            active_artifact_sha256 = UNSET
        else:
            active_artifact_sha256 = self.active_artifact_sha256

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if artifacts is not UNSET:
            field_dict["artifacts"] = artifacts
        if count is not UNSET:
            field_dict["count"] = count
        if active_artifact_sha256 is not UNSET:
            field_dict["active_artifact_sha256"] = active_artifact_sha256

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_artifact_summary_dto import PlaybookArtifactSummaryDTO

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        playbook_id = d.pop("playbook_id", UNSET)

        _artifacts = d.pop("artifacts", UNSET)
        artifacts: list[PlaybookArtifactSummaryDTO] | Unset = UNSET
        if _artifacts is not UNSET:
            artifacts = []
            for artifacts_item_data in _artifacts:
                artifacts_item = PlaybookArtifactSummaryDTO.from_dict(artifacts_item_data)

                artifacts.append(artifacts_item)

        count = d.pop("count", UNSET)

        def _parse_active_artifact_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        active_artifact_sha256 = _parse_active_artifact_sha256(d.pop("active_artifact_sha256", UNSET))

        list_playbook_artifacts_response = cls(
            success=success,
            playbook_id=playbook_id,
            artifacts=artifacts,
            count=count,
            active_artifact_sha256=active_artifact_sha256,
        )

        return list_playbook_artifacts_response
