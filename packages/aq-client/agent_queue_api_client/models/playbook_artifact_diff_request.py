from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookArtifactDiffRequest")


@_attrs_define
class PlaybookArtifactDiffRequest:
    """
    Attributes:
        playbook_id (str): The playbook that owns both artifacts.
        target_sha256 (str): The artifact under review, full 'sha256:<64 hex>' form.
        base_sha256 (None | str | Unset): The artifact to diff against. Defaults to the currently active artifact;
            absent entirely for a playbook's first artifact, which reports every element as added.
    """

    playbook_id: str
    target_sha256: str
    base_sha256: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        target_sha256 = self.target_sha256

        base_sha256: None | str | Unset
        if isinstance(self.base_sha256, Unset):
            base_sha256 = UNSET
        else:
            base_sha256 = self.base_sha256

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "target_sha256": target_sha256,
            }
        )
        if base_sha256 is not UNSET:
            field_dict["base_sha256"] = base_sha256

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        target_sha256 = d.pop("target_sha256")

        def _parse_base_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        base_sha256 = _parse_base_sha256(d.pop("base_sha256", UNSET))

        playbook_artifact_diff_request = cls(
            playbook_id=playbook_id,
            target_sha256=target_sha256,
            base_sha256=base_sha256,
        )

        playbook_artifact_diff_request.additional_properties = d
        return playbook_artifact_diff_request

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
