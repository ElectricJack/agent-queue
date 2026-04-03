from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ListPromptsRequest")


@_attrs_define
class ListPromptsRequest:
    """
    Attributes:
        project_id (str): Project ID
        category (None | str | Unset): Filter by category: system, task, hooks, or custom (optional)
        tag (None | str | Unset): Filter by tag (optional)
    """

    project_id: str
    category: None | str | Unset = UNSET
    tag: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        category: None | str | Unset
        if isinstance(self.category, Unset):
            category = UNSET
        else:
            category = self.category

        tag: None | str | Unset
        if isinstance(self.tag, Unset):
            tag = UNSET
        else:
            tag = self.tag

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if category is not UNSET:
            field_dict["category"] = category
        if tag is not UNSET:
            field_dict["tag"] = tag

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_category(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        category = _parse_category(d.pop("category", UNSET))

        def _parse_tag(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        tag = _parse_tag(d.pop("tag", UNSET))

        list_prompts_request = cls(
            project_id=project_id,
            category=category,
            tag=tag,
        )

        list_prompts_request.additional_properties = d
        return list_prompts_request

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
