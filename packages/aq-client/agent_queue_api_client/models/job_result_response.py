from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.job_result_response_result_type_0 import JobResultResponseResultType0


T = TypeVar("T", bound="JobResultResponse")


@_attrs_define
class JobResultResponse:
    """
    Attributes:
        result (JobResultResponseResultType0 | None):
        success (bool | Unset):  Default: True.
    """

    result: JobResultResponseResultType0 | None
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        from ..models.job_result_response_result_type_0 import JobResultResponseResultType0

        result: dict[str, Any] | None
        if isinstance(self.result, JobResultResponseResultType0):
            result = self.result.to_dict()
        else:
            result = self.result

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "result": result,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_result_response_result_type_0 import JobResultResponseResultType0

        d = dict(src_dict)

        def _parse_result(data: object) -> JobResultResponseResultType0 | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = JobResultResponseResultType0.from_dict(data)

                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(JobResultResponseResultType0 | None, data)

        result = _parse_result(d.pop("result"))

        success = d.pop("success", UNSET)

        job_result_response = cls(
            result=result,
            success=success,
        )

        return job_result_response
