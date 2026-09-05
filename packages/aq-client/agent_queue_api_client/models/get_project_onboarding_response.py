from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.get_project_onboarding_response_status import GetProjectOnboardingResponseStatus
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.onboard_project_response import OnboardProjectResponse
    from ..models.onboarding_error_info import OnboardingErrorInfo


T = TypeVar("T", bound="GetProjectOnboardingResponse")


@_attrs_define
class GetProjectOnboardingResponse:
    """
    Attributes:
        request_id (str):
        status (GetProjectOnboardingResponseStatus):
        success (bool | Unset):  Default: True.
        phase (None | str | Unset):
        result (None | OnboardProjectResponse | Unset):
        error (None | OnboardingErrorInfo | Unset):
        created_at (None | str | Unset):
        updated_at (None | str | Unset):
    """

    request_id: str
    status: GetProjectOnboardingResponseStatus
    success: bool | Unset = True
    phase: None | str | Unset = UNSET
    result: None | OnboardProjectResponse | Unset = UNSET
    error: None | OnboardingErrorInfo | Unset = UNSET
    created_at: None | str | Unset = UNSET
    updated_at: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.onboard_project_response import OnboardProjectResponse
        from ..models.onboarding_error_info import OnboardingErrorInfo

        request_id = self.request_id

        status = self.status.value

        success = self.success

        phase: None | str | Unset
        if isinstance(self.phase, Unset):
            phase = UNSET
        else:
            phase = self.phase

        result: dict[str, Any] | None | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, OnboardProjectResponse):
            result = self.result.to_dict()
        else:
            result = self.result

        error: dict[str, Any] | None | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        elif isinstance(self.error, OnboardingErrorInfo):
            error = self.error.to_dict()
        else:
            error = self.error

        created_at: None | str | Unset
        if isinstance(self.created_at, Unset):
            created_at = UNSET
        else:
            created_at = self.created_at

        updated_at: None | str | Unset
        if isinstance(self.updated_at, Unset):
            updated_at = UNSET
        else:
            updated_at = self.updated_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "request_id": request_id,
                "status": status,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if phase is not UNSET:
            field_dict["phase"] = phase
        if result is not UNSET:
            field_dict["result"] = result
        if error is not UNSET:
            field_dict["error"] = error
        if created_at is not UNSET:
            field_dict["created_at"] = created_at
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.onboard_project_response import OnboardProjectResponse
        from ..models.onboarding_error_info import OnboardingErrorInfo

        d = dict(src_dict)
        request_id = d.pop("request_id")

        status = GetProjectOnboardingResponseStatus(d.pop("status"))

        success = d.pop("success", UNSET)

        def _parse_phase(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        phase = _parse_phase(d.pop("phase", UNSET))

        def _parse_result(data: object) -> None | OnboardProjectResponse | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = OnboardProjectResponse.from_dict(data)

                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OnboardProjectResponse | Unset, data)

        result = _parse_result(d.pop("result", UNSET))

        def _parse_error(data: object) -> None | OnboardingErrorInfo | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                error_type_0 = OnboardingErrorInfo.from_dict(data)

                return error_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OnboardingErrorInfo | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_created_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        created_at = _parse_created_at(d.pop("created_at", UNSET))

        def _parse_updated_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        updated_at = _parse_updated_at(d.pop("updated_at", UNSET))

        get_project_onboarding_response = cls(
            request_id=request_id,
            status=status,
            success=success,
            phase=phase,
            result=result,
            error=error,
            created_at=created_at,
            updated_at=updated_at,
        )

        get_project_onboarding_response.additional_properties = d
        return get_project_onboarding_response

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
