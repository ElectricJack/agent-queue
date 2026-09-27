from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_profile_state import ProviderAllocationProfileState


T = TypeVar("T", bound="ProviderAllocationPreviewProfile")


@_attrs_define
class ProviderAllocationPreviewProfile:
    """One eligible profile of the provider, before and after the request.

    Attributes:
        profile_id (str):
        before (ProviderAllocationProfileState): The fields an allocation compares on one profile.
        after (ProviderAllocationProfileState): The fields an allocation compares on one profile.
        name (str | Unset):  Default: ''.
        harness (None | str | Unset):
        intelligence_class (None | str | Unset):
        selected (bool | Unset):  Default: False.
        changed (bool | Unset):  Default: False.
        changed_fields (list[str] | Unset):
    """

    profile_id: str
    before: ProviderAllocationProfileState
    after: ProviderAllocationProfileState
    name: str | Unset = ""
    harness: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    selected: bool | Unset = False
    changed: bool | Unset = False
    changed_fields: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id = self.profile_id

        before = self.before.to_dict()

        after = self.after.to_dict()

        name = self.name

        harness: None | str | Unset
        if isinstance(self.harness, Unset):
            harness = UNSET
        else:
            harness = self.harness

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        selected = self.selected

        changed = self.changed

        changed_fields: list[str] | Unset = UNSET
        if not isinstance(self.changed_fields, Unset):
            changed_fields = self.changed_fields

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
                "before": before,
                "after": after,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if harness is not UNSET:
            field_dict["harness"] = harness
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if selected is not UNSET:
            field_dict["selected"] = selected
        if changed is not UNSET:
            field_dict["changed"] = changed
        if changed_fields is not UNSET:
            field_dict["changed_fields"] = changed_fields

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_profile_state import ProviderAllocationProfileState

        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        before = ProviderAllocationProfileState.from_dict(d.pop("before"))

        after = ProviderAllocationProfileState.from_dict(d.pop("after"))

        name = d.pop("name", UNSET)

        def _parse_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        harness = _parse_harness(d.pop("harness", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        selected = d.pop("selected", UNSET)

        changed = d.pop("changed", UNSET)

        changed_fields = cast(list[str], d.pop("changed_fields", UNSET))

        provider_allocation_preview_profile = cls(
            profile_id=profile_id,
            before=before,
            after=after,
            name=name,
            harness=harness,
            intelligence_class=intelligence_class,
            selected=selected,
            changed=changed,
            changed_fields=changed_fields,
        )

        provider_allocation_preview_profile.additional_properties = d
        return provider_allocation_preview_profile

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
