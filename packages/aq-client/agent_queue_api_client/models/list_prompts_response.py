from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.list_prompts_response_prompts_item import ListPromptsResponsePromptsItem


T = TypeVar("T", bound="ListPromptsResponse")


@_attrs_define
class ListPromptsResponse:
    """
    Attributes:
        project_id (str):
        prompts (list[ListPromptsResponsePromptsItem] | Unset):
        categories (list[str] | Unset):
        total (int | Unset):  Default: 0.
    """

    project_id: str
    prompts: list[ListPromptsResponsePromptsItem] | Unset = UNSET
    categories: list[str] | Unset = UNSET
    total: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        prompts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.prompts, Unset):
            prompts = []
            for prompts_item_data in self.prompts:
                prompts_item = prompts_item_data.to_dict()
                prompts.append(prompts_item)

        categories: list[str] | Unset = UNSET
        if not isinstance(self.categories, Unset):
            categories = self.categories

        total = self.total

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if prompts is not UNSET:
            field_dict["prompts"] = prompts
        if categories is not UNSET:
            field_dict["categories"] = categories
        if total is not UNSET:
            field_dict["total"] = total

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.list_prompts_response_prompts_item import ListPromptsResponsePromptsItem

        d = dict(src_dict)
        project_id = d.pop("project_id")

        _prompts = d.pop("prompts", UNSET)
        prompts: list[ListPromptsResponsePromptsItem] | Unset = UNSET
        if _prompts is not UNSET:
            prompts = []
            for prompts_item_data in _prompts:
                prompts_item = ListPromptsResponsePromptsItem.from_dict(prompts_item_data)

                prompts.append(prompts_item)

        categories = cast(list[str], d.pop("categories", UNSET))

        total = d.pop("total", UNSET)

        list_prompts_response = cls(
            project_id=project_id,
            prompts=prompts,
            categories=categories,
            total=total,
        )

        list_prompts_response.additional_properties = d
        return list_prompts_response

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
