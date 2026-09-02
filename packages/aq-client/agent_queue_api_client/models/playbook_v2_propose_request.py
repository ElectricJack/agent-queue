from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookV2ProposeRequest")


@_attrs_define
class PlaybookV2ProposeRequest:
    """
    Attributes:
        playbook_id (str): Frontmatter id of the authoritative Markdown source.
        semantic_body_path (str): Path inside the vault to JSON containing only rules and steps.
        baseline_artifact_path (None | str | Unset): Optional prior V2 artifact for semantic diff and next version.
    """

    playbook_id: str
    semantic_body_path: str
    baseline_artifact_path: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        semantic_body_path = self.semantic_body_path

        baseline_artifact_path: None | str | Unset
        if isinstance(self.baseline_artifact_path, Unset):
            baseline_artifact_path = UNSET
        else:
            baseline_artifact_path = self.baseline_artifact_path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "semantic_body_path": semantic_body_path,
            }
        )
        if baseline_artifact_path is not UNSET:
            field_dict["baseline_artifact_path"] = baseline_artifact_path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        semantic_body_path = d.pop("semantic_body_path")

        def _parse_baseline_artifact_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        baseline_artifact_path = _parse_baseline_artifact_path(d.pop("baseline_artifact_path", UNSET))

        playbook_v2_propose_request = cls(
            playbook_id=playbook_id,
            semantic_body_path=semantic_body_path,
            baseline_artifact_path=baseline_artifact_path,
        )

        playbook_v2_propose_request.additional_properties = d
        return playbook_v2_propose_request

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
