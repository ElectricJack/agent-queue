from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.list_hook_runs_response_runs_item import ListHookRunsResponseRunsItem


T = TypeVar("T", bound="ListHookRunsResponse")


@_attrs_define
class ListHookRunsResponse:
    """
    Attributes:
        hook_id (str):
        hook_name (str | Unset):  Default: ''.
        runs (list[ListHookRunsResponseRunsItem] | Unset):
    """

    hook_id: str
    hook_name: str | Unset = ""
    runs: list[ListHookRunsResponseRunsItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        hook_id = self.hook_id

        hook_name = self.hook_name

        runs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.runs, Unset):
            runs = []
            for runs_item_data in self.runs:
                runs_item = runs_item_data.to_dict()
                runs.append(runs_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "hook_id": hook_id,
            }
        )
        if hook_name is not UNSET:
            field_dict["hook_name"] = hook_name
        if runs is not UNSET:
            field_dict["runs"] = runs

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.list_hook_runs_response_runs_item import ListHookRunsResponseRunsItem

        d = dict(src_dict)
        hook_id = d.pop("hook_id")

        hook_name = d.pop("hook_name", UNSET)

        _runs = d.pop("runs", UNSET)
        runs: list[ListHookRunsResponseRunsItem] | Unset = UNSET
        if _runs is not UNSET:
            runs = []
            for runs_item_data in _runs:
                runs_item = ListHookRunsResponseRunsItem.from_dict(runs_item_data)

                runs.append(runs_item)

        list_hook_runs_response = cls(
            hook_id=hook_id,
            hook_name=hook_name,
            runs=runs,
        )

        list_hook_runs_response.additional_properties = d
        return list_hook_runs_response

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
