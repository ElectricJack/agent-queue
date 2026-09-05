from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CiBaselineStatusRequest")


@_attrs_define
class CiBaselineStatusRequest:
    """
    Attributes:
        project_id (str): Project whose repository to read.
        ref (None | str | Unset): Branch or sha to judge. Default: the project's default branch.
        max_attempts (int | None | Unset): Repair attempts per failure signature before escalating. Default 2.
    """

    project_id: str
    ref: None | str | Unset = UNSET
    max_attempts: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        ref: None | str | Unset
        if isinstance(self.ref, Unset):
            ref = UNSET
        else:
            ref = self.ref

        max_attempts: int | None | Unset
        if isinstance(self.max_attempts, Unset):
            max_attempts = UNSET
        else:
            max_attempts = self.max_attempts

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if ref is not UNSET:
            field_dict["ref"] = ref
        if max_attempts is not UNSET:
            field_dict["max_attempts"] = max_attempts

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ref = _parse_ref(d.pop("ref", UNSET))

        def _parse_max_attempts(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_attempts = _parse_max_attempts(d.pop("max_attempts", UNSET))

        ci_baseline_status_request = cls(
            project_id=project_id,
            ref=ref,
            max_attempts=max_attempts,
        )

        ci_baseline_status_request.additional_properties = d
        return ci_baseline_status_request

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
