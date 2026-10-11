from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.gate_resolve_request_expected_type_0 import GateResolveRequestExpectedType0


T = TypeVar("T", bound="GateResolveRequest")


@_attrs_define
class GateResolveRequest:
    """
    Attributes:
        gate_id (str):
        expected (GateResolveRequestExpectedType0 | None | Unset):
        preview_id (None | str | Unset):
        idempotency_key (None | str | Unset):
        option_key (None | str | Unset):
        overall_note (None | str | Unset):
        resolved_by (None | str | Unset):
        resolution (None | str | Unset):
    """

    gate_id: str
    expected: GateResolveRequestExpectedType0 | None | Unset = UNSET
    preview_id: None | str | Unset = UNSET
    idempotency_key: None | str | Unset = UNSET
    option_key: None | str | Unset = UNSET
    overall_note: None | str | Unset = UNSET
    resolved_by: None | str | Unset = UNSET
    resolution: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.gate_resolve_request_expected_type_0 import GateResolveRequestExpectedType0

        gate_id = self.gate_id

        expected: dict[str, Any] | None | Unset
        if isinstance(self.expected, Unset):
            expected = UNSET
        elif isinstance(self.expected, GateResolveRequestExpectedType0):
            expected = self.expected.to_dict()
        else:
            expected = self.expected

        preview_id: None | str | Unset
        if isinstance(self.preview_id, Unset):
            preview_id = UNSET
        else:
            preview_id = self.preview_id

        idempotency_key: None | str | Unset
        if isinstance(self.idempotency_key, Unset):
            idempotency_key = UNSET
        else:
            idempotency_key = self.idempotency_key

        option_key: None | str | Unset
        if isinstance(self.option_key, Unset):
            option_key = UNSET
        else:
            option_key = self.option_key

        overall_note: None | str | Unset
        if isinstance(self.overall_note, Unset):
            overall_note = UNSET
        else:
            overall_note = self.overall_note

        resolved_by: None | str | Unset
        if isinstance(self.resolved_by, Unset):
            resolved_by = UNSET
        else:
            resolved_by = self.resolved_by

        resolution: None | str | Unset
        if isinstance(self.resolution, Unset):
            resolution = UNSET
        else:
            resolution = self.resolution

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "gate_id": gate_id,
            }
        )
        if expected is not UNSET:
            field_dict["expected"] = expected
        if preview_id is not UNSET:
            field_dict["preview_id"] = preview_id
        if idempotency_key is not UNSET:
            field_dict["idempotency_key"] = idempotency_key
        if option_key is not UNSET:
            field_dict["option_key"] = option_key
        if overall_note is not UNSET:
            field_dict["overall_note"] = overall_note
        if resolved_by is not UNSET:
            field_dict["resolved_by"] = resolved_by
        if resolution is not UNSET:
            field_dict["resolution"] = resolution

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.gate_resolve_request_expected_type_0 import GateResolveRequestExpectedType0

        d = dict(src_dict)
        gate_id = d.pop("gate_id")

        def _parse_expected(data: object) -> GateResolveRequestExpectedType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                expected_type_0 = GateResolveRequestExpectedType0.from_dict(data)

                return expected_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GateResolveRequestExpectedType0 | None | Unset, data)

        expected = _parse_expected(d.pop("expected", UNSET))

        def _parse_preview_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        preview_id = _parse_preview_id(d.pop("preview_id", UNSET))

        def _parse_idempotency_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        idempotency_key = _parse_idempotency_key(d.pop("idempotency_key", UNSET))

        def _parse_option_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        option_key = _parse_option_key(d.pop("option_key", UNSET))

        def _parse_overall_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        overall_note = _parse_overall_note(d.pop("overall_note", UNSET))

        def _parse_resolved_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        resolved_by = _parse_resolved_by(d.pop("resolved_by", UNSET))

        def _parse_resolution(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        resolution = _parse_resolution(d.pop("resolution", UNSET))

        gate_resolve_request = cls(
            gate_id=gate_id,
            expected=expected,
            preview_id=preview_id,
            idempotency_key=idempotency_key,
            option_key=option_key,
            overall_note=overall_note,
            resolved_by=resolved_by,
            resolution=resolution,
        )

        gate_resolve_request.additional_properties = d
        return gate_resolve_request

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
