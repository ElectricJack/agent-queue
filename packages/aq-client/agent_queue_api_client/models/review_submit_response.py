from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.review_submit_response_playbook_type_0 import ReviewSubmitResponsePlaybookType0


T = TypeVar("T", bound="ReviewSubmitResponse")


@_attrs_define
class ReviewSubmitResponse:
    """
    Attributes:
        review_id (str):
        revision (int):
        vault_path (str):
        success (bool | Unset):  Default: True.
        playbook (None | ReviewSubmitResponsePlaybookType0 | Unset):
    """

    review_id: str
    revision: int
    vault_path: str
    success: bool | Unset = True
    playbook: None | ReviewSubmitResponsePlaybookType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.review_submit_response_playbook_type_0 import ReviewSubmitResponsePlaybookType0

        review_id = self.review_id

        revision = self.revision

        vault_path = self.vault_path

        success = self.success

        playbook: dict[str, Any] | None | Unset
        if isinstance(self.playbook, Unset):
            playbook = UNSET
        elif isinstance(self.playbook, ReviewSubmitResponsePlaybookType0):
            playbook = self.playbook.to_dict()
        else:
            playbook = self.playbook

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
                "revision": revision,
                "vault_path": vault_path,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if playbook is not UNSET:
            field_dict["playbook"] = playbook

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.review_submit_response_playbook_type_0 import ReviewSubmitResponsePlaybookType0

        d = dict(src_dict)
        review_id = d.pop("review_id")

        revision = d.pop("revision")

        vault_path = d.pop("vault_path")

        success = d.pop("success", UNSET)

        def _parse_playbook(data: object) -> None | ReviewSubmitResponsePlaybookType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                playbook_type_0 = ReviewSubmitResponsePlaybookType0.from_dict(data)

                return playbook_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ReviewSubmitResponsePlaybookType0 | Unset, data)

        playbook = _parse_playbook(d.pop("playbook", UNSET))

        review_submit_response = cls(
            review_id=review_id,
            revision=revision,
            vault_path=vault_path,
            success=success,
            playbook=playbook,
        )

        review_submit_response.additional_properties = d
        return review_submit_response

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
