from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolScaleResponse")


@_attrs_define
class PoolScaleResponse:
    """
    Attributes:
        success (bool):
        project_id (None | str | Unset):
        profile_id (None | str | Unset):
        min_active (int | None | Unset):
        max_active (int | None | Unset):
        project_cap (int | None | Unset):
        effective_max_active (int | None | Unset):
        terminated (list[str] | Unset):
        error (None | str | Unset):
    """

    success: bool
    project_id: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    min_active: int | None | Unset = UNSET
    max_active: int | None | Unset = UNSET
    project_cap: int | None | Unset = UNSET
    effective_max_active: int | None | Unset = UNSET
    terminated: list[str] | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        min_active: int | None | Unset
        if isinstance(self.min_active, Unset):
            min_active = UNSET
        else:
            min_active = self.min_active

        max_active: int | None | Unset
        if isinstance(self.max_active, Unset):
            max_active = UNSET
        else:
            max_active = self.max_active

        project_cap: int | None | Unset
        if isinstance(self.project_cap, Unset):
            project_cap = UNSET
        else:
            project_cap = self.project_cap

        effective_max_active: int | None | Unset
        if isinstance(self.effective_max_active, Unset):
            effective_max_active = UNSET
        else:
            effective_max_active = self.effective_max_active

        terminated: list[str] | Unset = UNSET
        if not isinstance(self.terminated, Unset):
            terminated = self.terminated

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if min_active is not UNSET:
            field_dict["min_active"] = min_active
        if max_active is not UNSET:
            field_dict["max_active"] = max_active
        if project_cap is not UNSET:
            field_dict["project_cap"] = project_cap
        if effective_max_active is not UNSET:
            field_dict["effective_max_active"] = effective_max_active
        if terminated is not UNSET:
            field_dict["terminated"] = terminated
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_min_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        min_active = _parse_min_active(d.pop("min_active", UNSET))

        def _parse_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_active = _parse_max_active(d.pop("max_active", UNSET))

        def _parse_project_cap(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        project_cap = _parse_project_cap(d.pop("project_cap", UNSET))

        def _parse_effective_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        effective_max_active = _parse_effective_max_active(d.pop("effective_max_active", UNSET))

        terminated = cast(list[str], d.pop("terminated", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        pool_scale_response = cls(
            success=success,
            project_id=project_id,
            profile_id=profile_id,
            min_active=min_active,
            max_active=max_active,
            project_cap=project_cap,
            effective_max_active=effective_max_active,
            terminated=terminated,
            error=error,
        )

        pool_scale_response.additional_properties = d
        return pool_scale_response

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
