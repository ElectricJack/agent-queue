from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.job_error_response_result_type_0 import JobErrorResponseResultType0


T = TypeVar("T", bound="JobErrorResponse")


@_attrs_define
class JobErrorResponse:
    """
    Attributes:
        error (str):
        success (bool | Unset):  Default: False.
        error_code (None | str | Unset):
        result (JobErrorResponseResultType0 | None | Unset):
    """

    error: str
    success: bool | Unset = False
    error_code: None | str | Unset = UNSET
    result: JobErrorResponseResultType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.job_error_response_result_type_0 import JobErrorResponseResultType0

        error = self.error

        success = self.success

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        result: dict[str, Any] | None | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, JobErrorResponseResultType0):
            result = self.result.to_dict()
        else:
            result = self.result

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "error": error,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if result is not UNSET:
            field_dict["result"] = result

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_error_response_result_type_0 import JobErrorResponseResultType0

        d = dict(src_dict)
        error = d.pop("error")

        success = d.pop("success", UNSET)

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))

        def _parse_result(data: object) -> JobErrorResponseResultType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = JobErrorResponseResultType0.from_dict(data)

                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(JobErrorResponseResultType0 | None | Unset, data)

        result = _parse_result(d.pop("result", UNSET))

        job_error_response = cls(
            error=error,
            success=success,
            error_code=error_code,
            result=result,
        )

        job_error_response.additional_properties = d
        return job_error_response

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
