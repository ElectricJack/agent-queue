from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ObjectLoopReconcileRequest")


@_attrs_define
class ObjectLoopReconcileRequest:
    """
    Attributes:
        object_id (str):
        project_id (str):
        expected_version (int | None | Unset):
        next_variants (list[Any] | None | Unset):
        stop_reason (None | str | Unset):
    """

    object_id: str
    project_id: str
    expected_version: int | None | Unset = UNSET
    next_variants: list[Any] | None | Unset = UNSET
    stop_reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        object_id = self.object_id

        project_id = self.project_id

        expected_version: int | None | Unset
        if isinstance(self.expected_version, Unset):
            expected_version = UNSET
        else:
            expected_version = self.expected_version

        next_variants: list[Any] | None | Unset
        if isinstance(self.next_variants, Unset):
            next_variants = UNSET
        elif isinstance(self.next_variants, list):
            next_variants = self.next_variants

        else:
            next_variants = self.next_variants

        stop_reason: None | str | Unset
        if isinstance(self.stop_reason, Unset):
            stop_reason = UNSET
        else:
            stop_reason = self.stop_reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "object_id": object_id,
                "project_id": project_id,
            }
        )
        if expected_version is not UNSET:
            field_dict["expected_version"] = expected_version
        if next_variants is not UNSET:
            field_dict["next_variants"] = next_variants
        if stop_reason is not UNSET:
            field_dict["stop_reason"] = stop_reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        object_id = d.pop("object_id")

        project_id = d.pop("project_id")

        def _parse_expected_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_version = _parse_expected_version(d.pop("expected_version", UNSET))

        def _parse_next_variants(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                next_variants_type_0 = cast(list[Any], data)

                return next_variants_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        next_variants = _parse_next_variants(d.pop("next_variants", UNSET))

        def _parse_stop_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        stop_reason = _parse_stop_reason(d.pop("stop_reason", UNSET))

        object_loop_reconcile_request = cls(
            object_id=object_id,
            project_id=project_id,
            expected_version=expected_version,
            next_variants=next_variants,
            stop_reason=stop_reason,
        )

        object_loop_reconcile_request.additional_properties = d
        return object_loop_reconcile_request

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
