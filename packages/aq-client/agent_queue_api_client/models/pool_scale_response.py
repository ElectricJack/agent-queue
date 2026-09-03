from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pool_project_cap import PoolProjectCap


T = TypeVar("T", bound="PoolScaleResponse")


@_attrs_define
class PoolScaleResponse:
    """
    Attributes:
        success (bool):
        profile_id (None | str | Unset):
        min_active (int | None | Unset):
        max_active (int | None | Unset):
        project_caps (list[PoolProjectCap] | Unset):
        terminated (list[str] | Unset):
        warnings (list[str] | Unset):
        error (None | str | Unset):
    """

    success: bool
    profile_id: None | str | Unset = UNSET
    min_active: int | None | Unset = UNSET
    max_active: int | None | Unset = UNSET
    project_caps: list[PoolProjectCap] | Unset = UNSET
    terminated: list[str] | Unset = UNSET
    warnings: list[str] | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

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

        project_caps: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.project_caps, Unset):
            project_caps = []
            for project_caps_item_data in self.project_caps:
                project_caps_item = project_caps_item_data.to_dict()
                project_caps.append(project_caps_item)

        terminated: list[str] | Unset = UNSET
        if not isinstance(self.terminated, Unset):
            terminated = self.terminated

        warnings: list[str] | Unset = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = self.warnings

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
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if min_active is not UNSET:
            field_dict["min_active"] = min_active
        if max_active is not UNSET:
            field_dict["max_active"] = max_active
        if project_caps is not UNSET:
            field_dict["project_caps"] = project_caps
        if terminated is not UNSET:
            field_dict["terminated"] = terminated
        if warnings is not UNSET:
            field_dict["warnings"] = warnings
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pool_project_cap import PoolProjectCap

        d = dict(src_dict)
        success = d.pop("success")

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

        _project_caps = d.pop("project_caps", UNSET)
        project_caps: list[PoolProjectCap] | Unset = UNSET
        if _project_caps is not UNSET:
            project_caps = []
            for project_caps_item_data in _project_caps:
                project_caps_item = PoolProjectCap.from_dict(project_caps_item_data)

                project_caps.append(project_caps_item)

        terminated = cast(list[str], d.pop("terminated", UNSET))

        warnings = cast(list[str], d.pop("warnings", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        pool_scale_response = cls(
            success=success,
            profile_id=profile_id,
            min_active=min_active,
            max_active=max_active,
            project_caps=project_caps,
            terminated=terminated,
            warnings=warnings,
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
