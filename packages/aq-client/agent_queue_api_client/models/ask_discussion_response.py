from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_discussion_response_version import AskDiscussionResponseVersion


T = TypeVar("T", bound="AskDiscussionResponse")


@_attrs_define
class AskDiscussionResponse:
    """
    Attributes:
        created (bool):
        ask_id (str):
        input_id (str):
        entry_id (str):
        version (AskDiscussionResponseVersion):
        success (bool | Unset):  Default: True.
        delivery_id (None | str | Unset):
    """

    created: bool
    ask_id: str
    input_id: str
    entry_id: str
    version: AskDiscussionResponseVersion
    success: bool | Unset = True
    delivery_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        ask_id = self.ask_id

        input_id = self.input_id

        entry_id = self.entry_id

        version = self.version.to_dict()

        success = self.success

        delivery_id: None | str | Unset
        if isinstance(self.delivery_id, Unset):
            delivery_id = UNSET
        else:
            delivery_id = self.delivery_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
                "ask_id": ask_id,
                "input_id": input_id,
                "entry_id": entry_id,
                "version": version,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if delivery_id is not UNSET:
            field_dict["delivery_id"] = delivery_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_discussion_response_version import AskDiscussionResponseVersion

        d = dict(src_dict)
        created = d.pop("created")

        ask_id = d.pop("ask_id")

        input_id = d.pop("input_id")

        entry_id = d.pop("entry_id")

        version = AskDiscussionResponseVersion.from_dict(d.pop("version"))

        success = d.pop("success", UNSET)

        def _parse_delivery_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        delivery_id = _parse_delivery_id(d.pop("delivery_id", UNSET))

        ask_discussion_response = cls(
            created=created,
            ask_id=ask_id,
            input_id=input_id,
            entry_id=entry_id,
            version=version,
            success=success,
            delivery_id=delivery_id,
        )

        ask_discussion_response.additional_properties = d
        return ask_discussion_response

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
