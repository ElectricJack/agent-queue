from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationPreference")


@_attrs_define
class ProviderAllocationPreference:
    """A project's preferred provider for unpinned work, before and after.

    Attributes:
        project_id (str):
        mode (str):
        before (None | str | Unset):
        after (None | str | Unset):
        changed (bool | Unset):  Default: False.
    """

    project_id: str
    mode: str
    before: None | str | Unset = UNSET
    after: None | str | Unset = UNSET
    changed: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        mode = self.mode

        before: None | str | Unset
        if isinstance(self.before, Unset):
            before = UNSET
        else:
            before = self.before

        after: None | str | Unset
        if isinstance(self.after, Unset):
            after = UNSET
        else:
            after = self.after

        changed = self.changed

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "mode": mode,
            }
        )
        if before is not UNSET:
            field_dict["before"] = before
        if after is not UNSET:
            field_dict["after"] = after
        if changed is not UNSET:
            field_dict["changed"] = changed

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        mode = d.pop("mode")

        def _parse_before(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        before = _parse_before(d.pop("before", UNSET))

        def _parse_after(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        after = _parse_after(d.pop("after", UNSET))

        changed = d.pop("changed", UNSET)

        provider_allocation_preference = cls(
            project_id=project_id,
            mode=mode,
            before=before,
            after=after,
            changed=changed,
        )

        provider_allocation_preference.additional_properties = d
        return provider_allocation_preference

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
