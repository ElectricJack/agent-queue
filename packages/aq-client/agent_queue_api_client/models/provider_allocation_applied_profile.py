from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_profile_state import ProviderAllocationProfileState


T = TypeVar("T", bound="ProviderAllocationAppliedProfile")


@_attrs_define
class ProviderAllocationAppliedProfile:
    """One changed profile and what apply did to it.

    ``status``: ``applied``, ``failed``, ``rolled_back`` (applied, then
    compensated), ``rollback_failed`` or ``skipped`` (never reached after an
    earlier failure).  ``before`` / ``after`` are the previewed rows.

        Attributes:
            profile_id (str):
            status (str):
            before (ProviderAllocationProfileState): The fields an allocation compares on one profile.
            after (ProviderAllocationProfileState): The fields an allocation compares on one profile.
            changed_fields (list[str] | Unset):
            error (None | str | Unset):
            compensated (bool | None | Unset):
            compensation_error (None | str | Unset):
    """

    profile_id: str
    status: str
    before: ProviderAllocationProfileState
    after: ProviderAllocationProfileState
    changed_fields: list[str] | Unset = UNSET
    error: None | str | Unset = UNSET
    compensated: bool | None | Unset = UNSET
    compensation_error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        profile_id = self.profile_id

        status = self.status

        before = self.before.to_dict()

        after = self.after.to_dict()

        changed_fields: list[str] | Unset = UNSET
        if not isinstance(self.changed_fields, Unset):
            changed_fields = self.changed_fields

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        compensated: bool | None | Unset
        if isinstance(self.compensated, Unset):
            compensated = UNSET
        else:
            compensated = self.compensated

        compensation_error: None | str | Unset
        if isinstance(self.compensation_error, Unset):
            compensation_error = UNSET
        else:
            compensation_error = self.compensation_error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
                "status": status,
                "before": before,
                "after": after,
            }
        )
        if changed_fields is not UNSET:
            field_dict["changed_fields"] = changed_fields
        if error is not UNSET:
            field_dict["error"] = error
        if compensated is not UNSET:
            field_dict["compensated"] = compensated
        if compensation_error is not UNSET:
            field_dict["compensation_error"] = compensation_error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_profile_state import ProviderAllocationProfileState

        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        status = d.pop("status")

        before = ProviderAllocationProfileState.from_dict(d.pop("before"))

        after = ProviderAllocationProfileState.from_dict(d.pop("after"))

        changed_fields = cast(list[str], d.pop("changed_fields", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_compensated(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        compensated = _parse_compensated(d.pop("compensated", UNSET))

        def _parse_compensation_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        compensation_error = _parse_compensation_error(d.pop("compensation_error", UNSET))

        provider_allocation_applied_profile = cls(
            profile_id=profile_id,
            status=status,
            before=before,
            after=after,
            changed_fields=changed_fields,
            error=error,
            compensated=compensated,
            compensation_error=compensation_error,
        )

        provider_allocation_applied_profile.additional_properties = d
        return provider_allocation_applied_profile

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
