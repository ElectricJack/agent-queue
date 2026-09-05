from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.onboarding_error_info_details import OnboardingErrorInfoDetails
    from ..models.onboarding_error_info_field_errors_item import OnboardingErrorInfoFieldErrorsItem


T = TypeVar("T", bound="OnboardingErrorInfo")


@_attrs_define
class OnboardingErrorInfo:
    """
    Attributes:
        error_code (str):
        error (str):
        phase (None | str | Unset):
        details (OnboardingErrorInfoDetails | Unset):
        field_errors (list[OnboardingErrorInfoFieldErrorsItem] | Unset):
    """

    error_code: str
    error: str
    phase: None | str | Unset = UNSET
    details: OnboardingErrorInfoDetails | Unset = UNSET
    field_errors: list[OnboardingErrorInfoFieldErrorsItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        error_code = self.error_code

        error = self.error

        phase: None | str | Unset
        if isinstance(self.phase, Unset):
            phase = UNSET
        else:
            phase = self.phase

        details: dict[str, Any] | Unset = UNSET
        if not isinstance(self.details, Unset):
            details = self.details.to_dict()

        field_errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.field_errors, Unset):
            field_errors = []
            for field_errors_item_data in self.field_errors:
                field_errors_item = field_errors_item_data.to_dict()
                field_errors.append(field_errors_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "error_code": error_code,
                "error": error,
            }
        )
        if phase is not UNSET:
            field_dict["phase"] = phase
        if details is not UNSET:
            field_dict["details"] = details
        if field_errors is not UNSET:
            field_dict["field_errors"] = field_errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.onboarding_error_info_details import OnboardingErrorInfoDetails
        from ..models.onboarding_error_info_field_errors_item import OnboardingErrorInfoFieldErrorsItem

        d = dict(src_dict)
        error_code = d.pop("error_code")

        error = d.pop("error")

        def _parse_phase(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        phase = _parse_phase(d.pop("phase", UNSET))

        _details = d.pop("details", UNSET)
        details: OnboardingErrorInfoDetails | Unset
        if isinstance(_details, Unset):
            details = UNSET
        else:
            details = OnboardingErrorInfoDetails.from_dict(_details)

        _field_errors = d.pop("field_errors", UNSET)
        field_errors: list[OnboardingErrorInfoFieldErrorsItem] | Unset = UNSET
        if _field_errors is not UNSET:
            field_errors = []
            for field_errors_item_data in _field_errors:
                field_errors_item = OnboardingErrorInfoFieldErrorsItem.from_dict(field_errors_item_data)

                field_errors.append(field_errors_item)

        onboarding_error_info = cls(
            error_code=error_code,
            error=error,
            phase=phase,
            details=details,
            field_errors=field_errors,
        )

        onboarding_error_info.additional_properties = d
        return onboarding_error_info

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
